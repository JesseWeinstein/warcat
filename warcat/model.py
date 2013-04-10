'''Document model'''
# Copyright 2013 Christopher Foo <chris.foo@gmail.com>
# Licensed under GPLv3. See COPYING.txt for details.
from warcat import util
import abc
import collections
import gzip
import isodate
import logging
import re

NEWLINE = '\r\n'
NEWLINE_BYTES = b'\r\n'
FIELD_DELIM_BYTES = NEWLINE_BYTES * 2

_logger = logging.getLogger(__name__)


class BytesSerializable(metaclass=abc.ABCMeta):
    '''Metaclass that indicates this object can be serialized to bytes'''

    @abc.abstractmethod
    def iter_bytes(self):
        '''Return an iterable of bytes'''
        pass

    def __bytes__(self):
        return b''.join(self.iter_bytes())


class StrSerializable(metaclass=abc.ABCMeta):
    '''Metaclass that indicates this object can be serialized to str'''

    @abc.abstractmethod
    def iter_str(self):
        '''Return an iterable of str'''
        pass

    def __str__(self):
        return ''.join(self.iter_str())


class BinaryFileRef(metaclass=abc.ABCMeta):
    '''Reference to a file containing the content block data'''

    def __init__(self):
        self.file_offset = None
        self.length = None
        self.filename = None

    def set_source_file(self, filename, offset=0, length=None):
        '''Set the reference to the file with the data'''

        self.filename = filename
        self.file_offset = offset
        self.length = length

    def iter_bytes_over_source_file(self):
        '''Return an iterable of bytes of the source data'''

        if hasattr(self.filename, 'name'):
            filename = self.filename.name
        else:
            filename = self.filename

        file_obj = util.file_cache.get(filename)

        if not file_obj:
            if filename.endswith('.gz'):
                file_obj = util.DiskBufferedReader(gzip.GzipFile(filename))
            else:
                file_obj = open(filename, 'rb')

            util.file_cache.put(filename, file_obj)

        if self.file_offset:
            file_obj.seek(self.file_offset)

        bytes_read = 0

        while True:
            if self.length is not None:
                length = min(4096, self.length - bytes_read)
            else:
                length = 4096

            data = file_obj.read(length)
            bytes_read += len(data)

            if not data or not length:
                break

            yield data


class Fields(StrSerializable, BytesSerializable):
    '''Name and value pseudo-map list

    Behaves like a `dict` or mutable mapping. Mutable mapping operations
    remove any duplicates in the field list.
    '''

    def __init__(self, field_list=None):
        self._list = [] if field_list is None else field_list

    def __contains__(self, name):
        return self[name] is not None

    def __iter__(self):
        return self._list

    def __len__(self):
        return len(self._list)

    def __getitem__(self, name):
        for k, v in self._list:
            if k.lower() == name.lower():
                return v

    def __setitem__(self, name, value):
        try:
            index = self.index(name)
        except KeyError:
            self._list.append((name, value))
        else:
            del self[name]
            self._list.insert(index, (name, value))

    def __delitem__(self, name):
        self._list[:] = [x for x in self._list if x[0].lower() != name.lower()]

    def add(self, name, value):
        '''Append a name-value field to the list'''
        self._list.append((name, value))

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def get_list(self, name):
        '''Return a list of values'''

        return list([x for x in self._list if x[0].lower() == name.lower()])

    def count(self, name):
        '''Count the number of times this name occurs in the list'''

        return len(self.get_list(name))

    def index(self, name):
        '''Return the index of the first occurance of given name'''

        for i in range(len(self._list)):
            if self._list[i][0].lower() == name.lower():
                return i

        raise KeyError('Name {} not found in fields'.format(name))

    def list(self):
        '''Return the underlying list'''

        return self._list

    def keys(self):
        return [x[0] for x in self._list]

    def values(self):
        return [x[1] for x in self._list]

    def clear(self):
        self._list[:] = []

    def iter_str(self):
        for name, value in self._list:
            yield '{}: {}'.format(name, value)
            yield NEWLINE

    def iter_bytes(self):
        for s in self.iter_str():
            yield s.encode()

    @classmethod
    def parse(cls, s, newline=NEWLINE):
        '''Parse a named field string and return a :class:`Fields`'''

        fields = Fields()
        lines = collections.deque(re.split(newline, s))

        while len(lines):
            line = lines.popleft()

            if line == '':
                continue

            name, value = line.split(':', 1)
            value = value.lstrip()
            value = cls.join_multilines(value, lines)
            fields.add(name, value)

        return fields

    @classmethod
    def join_multilines(cls, value, lines):
        '''Scan for multiline value which is prefixed with a space or tab'''

        while len(lines):
            line = lines.popleft()

            if line == '':
                break
            if line[0] not in (' ', '\t'):
                lines.appendleft(line)
                break

            value = '{}{}'.format(value, line[1:])

        return value


class WARC(BytesSerializable):
    '''A Web ARChive file model.

    Typically, large streaming operations should use :func:`open` and
    :func:`read_record` functions.
    '''

    def __init__(self):
        self.records = []

    def load(self, filename):
        '''Open and load the contents of the given filename.

        The records are located in :attr:`records`.
        '''

        f = self.open(filename)
        self.read_file_object(f)
        f.close()

    def read_file_object(self, file_object):
        '''Read records until the file object is exhausted'''

        while True:
            record, has_more = self.read_record(file_object)
            self.records.append(record)
            if not has_more:
                break

    @classmethod
    def open(cls, filename, force_gzip=False):
        '''Return a logical file object.

        :param filename: The path of the file. gzip compression is detected
            using file extension.
        :param force_gzip: Use gzip compression always.
        '''

        if filename.endswith('.gz') or force_gzip:
            f = gzip.open(filename)
            _logger.info('Opened gziped file %s', filename)
            return util.DiskBufferedReader(f)
        else:
            f = open(filename, 'rb')
            _logger.info('Opened file %s', filename)
            return f

    @classmethod
    def read_record(cls, file_object, preserve_block=False):
        '''Return a record and whether there are more records to read.

        .. seealso:: :class:`Record`

        :return: A tuple. The first item is the :class:`Record`. The second
            item is a boolean indicating whether there are more records to
            be read.
        '''

        record = Record.load(file_object, preserve_block=preserve_block)
        _logger.debug('Finished reading a record %s', record.record_id)

        data = file_object.read(len(FIELD_DELIM_BYTES))

        if data != FIELD_DELIM_BYTES:
            _logger.debug('Wrong delim %s', data)
            raise IOError('Blocks not separated correctly (tell={})'.format(
                file_object.tell()))

        if not file_object.peek(1):
            _logger.info('Finished reading Warc')
            return (record, False)
        else:
            return (record, True)

    def iter_bytes(self):
        for record in self.records:
            for v in record.iter_bytes():
                yield v


class Record(BytesSerializable):
    '''A WARC Record within a WARC file.


    .. attribute:: header

        :class:`Header`

    .. attribute:: content_block

        A :class:`BinaryBlock` or :class:`BlockWithPayload`

    .. attribute:: file_offset

        An `int` descripting the location of the record in the file.
    '''

    def __init__(self):
        self.header = Header()
        self.content_block = None
        self.file_offset = None

    @classmethod
    def load(cls, file_obj, preserve_block=False):
        '''Parse and return a :class:`Record`

        :param file_object: A file-like object.
        :param preserve_block: If `True`, content blocks are not parsed
            for fields and payloads. Enabling this feature ensures
            preservation of content length and hash digests.
        '''

        _logger.debug('Record start at %d 0x%x', file_obj.tell(),
            file_obj.tell())

        record = Record()
        record.file_offset = file_obj.tell()
        header_length = util.find_file_pattern(file_obj, FIELD_DELIM_BYTES,
            inclusive=True)
        record.header = Header.parse(file_obj.read(header_length))
        block_length = record.content_length

        _logger.debug('Block length=%d', block_length)

        content_type = record.header.fields.get('content-type')

        if not preserve_block and content_type \
        and content_type.startswith('application/http'):
            record.content_block = BlockWithPayload.load(file_obj,
                block_length, field_cls=HTTPHeaders)
        elif not preserve_block and content_type == 'application/warc-fields':
            record.content_block = BlockWithPayload.load(file_obj,
                block_length, field_cls=Fields)
        else:
            record.content_block = BinaryBlock.load(file_obj, block_length)

        new_content_length = record.content_block.length

        if block_length != new_content_length:
            _logger.warn('Content block length changed from %d to %d',
                record.content_length, new_content_length)
            record.content_length = new_content_length

        return record

    @property
    def record_id(self):
        return self.header.fields['WARC-Record-ID']

    @record_id.setter
    def record_id(self, s):
        self.header.fields['WARC-Record-ID'] = s

    @property
    def content_length(self):
        return int(self.header.fields['Content-Length'])

    @content_length.setter
    def content_length(self, i):
        self.header.fields['Content-Length'] = int(i)

    @property
    def date(self):
        return isodate.parse_datetime(self.header.fields['WARC-Date'])

    @date.setter
    def date(self, datetime_obj):
        self.header.fields['WARC-Date'] = isodate.datetime_isoformat(
            datetime_obj)

    @property
    def warc_type(self):
        return self.header.fields['WARC-Type']

    @warc_type.setter
    def warc_type(self, s):
        self.header.fields['WARC-Type'] = s

    def iter_bytes(self):
        _logger.debug('Iter bytes on record %s', self.record_id)

        for v in self.header.iter_bytes():
            yield v

        if self.content_block:
            for v in self.content_block.iter_bytes():
                yield v

        yield NEWLINE_BYTES
        yield NEWLINE_BYTES


class ContentBlock(BytesSerializable):
    pass


class BinaryBlock(ContentBlock, BinaryFileRef):
    '''A content block that is octet data'''

    def iter_bytes(self):
        for v in self.iter_bytes_over_source_file():
            yield v

    @classmethod
    def load(cls, file_obj, length):
        '''Return a :class:`BinaryBlock` using given file object'''

        binary_block = BinaryBlock()
        binary_block.set_source_file(file_obj, offset=file_obj.tell(),
            length=length)

        file_obj.seek(file_obj.tell() + length)

        _logger.debug('Binary content block length=%d', binary_block.length)

        return binary_block


class BlockWithPayload(ContentBlock):
    '''A content block (fields/data) within a :class:`Record`.

    .. attribute:: fields

        :class:`Fields`

    .. attribute:: payload

        :class:`Payload`
    '''

    def __init__(self):
        self.fields = None
        self.payload = None

    @classmethod
    def load(cls, file_obj, length, field_cls):
        '''Return a :class:`BlockWithPayload`

        :param file_obj: The file object
        :param length: How much to read from the file
        :param field_cls: The class or subclass of :class:`Fields`
        '''

        content_block = BlockWithPayload()

        try:
            field_length = util.find_file_pattern(file_obj, FIELD_DELIM_BYTES,
                limit=length, inclusive=True)
        except ValueError:
            # No payload
            field_length = length

        content_block.fields = field_cls.parse(
            file_obj.read(field_length).decode())

        payload_length = length - field_length

        content_block.payload = Payload()
        content_block.payload.set_source_file(file_obj,
            offset=file_obj.tell(), length=payload_length)
        _logger.debug('Field length=%d', field_length)
        _logger.debug('Payload length=%d', payload_length)

        file_obj.seek(file_obj.tell() + payload_length)
        return content_block

    @property
    def length(self):
        '''Return the new computed length'''

        return (len(bytes(self.fields)) + len(NEWLINE_BYTES) +
            self.payload.length)

    def iter_bytes(self):
        for v in self.fields.iter_bytes():
            yield v

        yield NEWLINE_BYTES

        for v in self.payload.iter_bytes():
            yield v


class Payload(BytesSerializable, BinaryFileRef):
    '''Data within a content block that has fields'''

    def __init__(self):
        BinaryFileRef.__init__(self)

    def iter_bytes(self):
        for v in self.iter_bytes_over_source_file():
            yield v


class Header(StrSerializable, BytesSerializable):
    '''A header of a WARC Record.

    .. attribute:: version

        A `str` containing the version

    .. attribute:: fields

        The :class:`Fields` object.
    '''

    def __init__(self):
        self.version = None
        self.fields = None

    @classmethod
    def parse(cls, b):
        '''Parse from `bytes` and return :class:`Header`'''

        header = Header()
        version_line, field_str = b.decode().split(NEWLINE, 1)

        _logger.debug('Version line=%s', version_line)

        if not version_line.startswith('WARC'):
            raise IOError('Wrong WARC header')

        header.version = version_line[5:]
        header.fields = Fields.parse(field_str)

        return header

    def iter_str(self):
        yield 'WARC/'
        yield self.version
        yield NEWLINE

        for v in self.fields.iter_str():
            yield v

        yield NEWLINE

    def iter_bytes(self):
        for s in self.iter_str():
            yield s.encode()


class HTTPHeaders(Fields):
    '''Fields extended with a HTTP status attribute.

    .. attribute:: status

        The `str` of the HTTP status message and code.
    '''

    def __init__(self, *args):
        Fields.__init__(self, *args)
        self.status = None

    @classmethod
    def parse(cls, s, newline=NEWLINE):
        http_headers = HTTPHeaders()
        http_headers.status, s = s.split(newline, 1)

        fields = super(HTTPHeaders, cls).parse(s, newline=newline)
        http_headers.list().extend(fields.list())

        return http_headers

    def iter_str(self):
        yield self.status
        yield NEWLINE

        for s in Fields.iter_str(self):
            yield s
