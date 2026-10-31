import io
from pathlib import Path

import pytest

from loader.multipart import HTTPError, parse
from test_runtime import multipart


class TinyReads(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 7))


def test_binary_boundaries_and_bounded_stream(tmp_path):
    data, content_type = multipart(dict(design_zip=b'\x00\xff\r\n--not-a-boundary\r\n'*10000, request_id='r'))
    fields = parse(TinyReads(data), len(data), content_type, tmp_path)
    assert fields['design_zip'].read_bytes() == b'\x00\xff\r\n--not-a-boundary\r\n'*10000
    assert fields['request_id'] == 'r'


def test_near_boundary_in_binary(tmp_path):
    b = b'\x00\r\n--testX!binary\r\n--test-!more'
    data = b'--test\r\nContent-Disposition: form-data; name="design_zip"\r\n\r\n' + b + b'\r\n--test--\r\n'
    fields = parse(TinyReads(data), len(data), 'multipart/form-data; boundary=test', tmp_path)
    assert fields['design_zip'].read_bytes() == b


@pytest.mark.parametrize('data', [
    b'--b\r\nContent-Disposition: form-data; name="unknown"\r\n\r\nx\r\n--b--\r\n',
    b'--b\r\nContent-Disposition: form-data; name="request_id"\r\n\r\na\r\n--b\r\nContent-Disposition: form-data; name="request_id"\r\n\r\nb\r\n--b--\r\n',
    b'--b\r\nContent-Disposition: form-data; name="design_zip"\r\nContent-Transfer-Encoding: base64\r\n\r\neA==\r\n--b--\r\n',
    b'--b\r\nContent-Disposition: form-data; name="design_zip"\r\n\r\nx',
])
def test_malformed_duplicate_unknown_encoding_refused(tmp_path, data):
    with pytest.raises(HTTPError):
        parse(io.BytesIO(data), len(data), 'multipart/form-data; boundary=b', tmp_path)
