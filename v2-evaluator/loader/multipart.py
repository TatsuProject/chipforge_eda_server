"""Bounded streaming multipart/form-data for ZIP uploads (stdlib only)."""
from email.message import Message
from pathlib import Path
import re
import tempfile


class HTTPError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class Reader:
    def __init__(self, stream, length):
        self.stream, self.remaining, self.buffer = stream, length, b''

    def fill(self):
        if not self.remaining:
            return False
        data = self.stream.read(min(64 << 10, self.remaining))
        if not data:
            raise HTTPError(400, 'TRUNCATED_UPLOAD', 'Upload ended early.')
        self.remaining -= len(data)
        self.buffer += data
        return True

    def line(self, limit=8192):
        while b'\r\n' not in self.buffer:
            if len(self.buffer) > limit or not self.fill():
                raise HTTPError(400, 'INVALID_MULTIPART', 'Malformed multipart headers.')
        line, self.buffer = self.buffer.split(b'\r\n', 1)
        if len(line) > limit:
            raise HTTPError(400, 'INVALID_MULTIPART', 'Multipart header too long.')
        return line

    def part(self, boundary, output, limit):
        marker = b'\r\n--' + boundary
        count = 0
        while True:
            index = self.buffer.find(marker)
            if index >= 0 and len(self.buffer) < index + len(marker) + 2:
                if self.fill():
                    continue
            if index >= 0 and self.buffer[index + len(marker):index + len(marker) + 2] in (b'--', b'\r\n'):
                data, self.buffer = self.buffer[:index], self.buffer[index + 2:]
                count += len(data)
                if count > limit:
                    raise HTTPError(413, 'FIELD_TOO_LARGE', 'Multipart field exceeds its limit.')
                output.write(data)
                return
            # Retain enough to recognize a delimiter split across read boundaries.
            size = max(0, len(self.buffer) - len(marker) - 2)
            if index >= 0:
                size = max(1, index + 2)  # Boundary-like binary content with an invalid suffix.
            if size:
                data, self.buffer = self.buffer[:size], self.buffer[size:]
                count += len(data)
                if count > limit:
                    raise HTTPError(413, 'FIELD_TOO_LARGE', 'Multipart field exceeds its limit.')
                output.write(data)
            if not self.fill():
                raise HTTPError(400, 'INVALID_MULTIPART', 'Multipart closing boundary missing.')


def parse(stream, length, content_type, directory, evaluate=True):
    msg = Message()
    msg['content-type'] = content_type
    boundary = msg.get_param('boundary')
    if msg.get_content_type() != 'multipart/form-data' or not boundary or not re.fullmatch(r'[ -~]{1,70}', boundary):
        raise HTTPError(415, 'MULTIPART_REQUIRED', 'Use multipart/form-data with a boundary.')
    boundary = boundary.encode('ascii')
    reader, fields = Reader(stream, length), {}
    delimiter = b'--' + boundary
    if reader.line(80) != delimiter:
        raise HTTPError(400, 'INVALID_MULTIPART', 'Invalid initial boundary.')
    allowed = {'evaluator_zip', 'evaluator_sha256', 'evaluator_sig', 'signature'}
    if evaluate:
        allowed |= {'design_zip', 'request_id', 'mode', 'submitter', 'chain_commit'}
    while True:
        headers = Message()
        for _ in range(16):
            line = reader.line()
            if not line:
                break
            key, sep, value = line.partition(b':')
            if not sep or not re.fullmatch(rb'[A-Za-z-]+', key) or key.decode().lower() in headers:
                raise HTTPError(400, 'INVALID_MULTIPART', 'Invalid multipart header.')
            headers[key.decode()] = value.decode('ascii').strip()
        else:
            raise HTTPError(400, 'INVALID_MULTIPART', 'Too many multipart headers.')
        name = headers.get_param('name', header='content-disposition')
        if headers.get_content_disposition() != 'form-data' or name not in allowed or name in fields:
            raise HTTPError(400, 'INVALID_FIELD', 'Unknown or duplicate multipart field.')
        if headers.get('content-transfer-encoding', 'binary').lower() not in ('binary', '7bit', '8bit'):
            raise HTTPError(400, 'INVALID_ENCODING', 'Transfer encoded fields are unsupported.')
        fd, tmp = tempfile.mkstemp(dir=directory)
        import os
        with os.fdopen(fd, 'wb') as out:
            reader.part(boundary, out, (256 << 20) if name.endswith('_zip') else (16 << 10) if name in ('evaluator_sig', 'signature')
                        else 2048 if name == 'chain_commit' else 128)
        p = Path(tmp)
        if name.endswith('_zip'):
            fields[name] = p
        else:
            data = p.read_bytes()
            p.unlink()
            fields[name] = data if name in ('evaluator_sig', 'signature') else data.decode('ascii')
        line = reader.line(80)
        if line == delimiter + b'--':
            while reader.fill():
                if len(reader.buffer) > 2:
                    raise HTTPError(400, 'INVALID_MULTIPART', 'Unexpected multipart epilogue.')
            if reader.buffer not in (b'', b'\r\n'):
                raise HTTPError(400, 'INVALID_MULTIPART', 'Unexpected multipart epilogue.')
            return fields
        if line != delimiter:
            raise HTTPError(400, 'INVALID_MULTIPART', 'Invalid multipart boundary.')
