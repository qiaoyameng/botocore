# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Unit tests for the binary event stream decoder."""

from binascii import crc32

import pytest

from botocore.eventstream import (
    ChecksumMismatch,
    DecodeUtils,
    DuplicateHeader,
    EventStream,
    EventStreamBuffer,
    EventStreamHeaderParser,
    EventStreamMessage,
    InvalidHeadersLength,
    InvalidPayloadLength,
    MessagePrelude,
    NoInitialResponseError,
    UnknownEventStreamEvent,
)
from botocore.exceptions import EventStreamError
from botocore.parsers import EventStreamXMLParser, ResponseParserError
from tests import mock

EMPTY_MESSAGE = (
    b'\x00\x00\x00\x10\x00\x00\x00\x00\x05\xc2H\xeb}\x98\xc8\xff',
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x10,
            headers_length=0,
            crc=0x05C248EB,
        ),
        headers={},
        payload=b'',
        crc=0x7D98C8FF,
    ),
)

INT8_HEADER = (
    (
        b"\x00\x00\x00\x17\x00\x00\x00\x07)\x86\x01X\x04"
        b"byte\x02\xff\xc2\xf8i\xdc"
    ),
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x17,
            headers_length=0x7,
            crc=0x29860158,
        ),
        headers={'byte': -1},
        payload=b'',
        crc=0xC2F869DC,
    ),
)

INT16_HEADER = (
    (
        b"\x00\x00\x00\x19\x00\x00\x00\tq\x0e\x92>\x05"
        b"short\x03\xff\xff\xb2|\xb6\xcc"
    ),
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x19,
            headers_length=0x9,
            crc=0x710E923E,
        ),
        headers={'short': -1},
        payload=b'',
        crc=0xB27CB6CC,
    ),
)

INT32_HEADER = (
    (
        b"\x00\x00\x00\x1d\x00\x00\x00\r\x83\xe3\xf0\xe7\x07"
        b"integer\x04\xff\xff\xff\xff\x8b\x8e\x12\xeb"
    ),
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x1D,
            headers_length=0xD,
            crc=0x83E3F0E7,
        ),
        headers={'integer': -1},
        payload=b'',
        crc=0x8B8E12EB,
    ),
)

INT64_HEADER = (
    (
        b"\x00\x00\x00\x1e\x00\x00\x00\x0e]J\xdb\x8d\x04"
        b"long\x05\xff\xff\xff\xff\xff\xff\xff\xffK\xc22\xda"
    ),
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x1E,
            headers_length=0xE,
            crc=0x5D4ADB8D,
        ),
        headers={'long': -1},
        payload=b'',
        crc=0x4BC232DA,
    ),
)

PAYLOAD_NO_HEADERS = (
    b"\x00\x00\x00\x1d\x00\x00\x00\x00\xfdR\x8cZ{'foo':'bar'}\xc3e96",
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x1D,
            headers_length=0,
            crc=0xFD528C5A,
        ),
        headers={},
        payload=b"{'foo':'bar'}",
        crc=0xC3653936,
    ),
)

PAYLOAD_ONE_STR_HEADER = (
    (
        b"\x00\x00\x00=\x00\x00\x00 \x07\xfd\x83\x96\x0ccontent-type\x07\x00\x10"
        b"application/json{'foo':'bar'}\x8d\x9c\x08\xb1"
    ),
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x3D,
            headers_length=0x20,
            crc=0x07FD8396,
        ),
        headers={'content-type': 'application/json'},
        payload=b"{'foo':'bar'}",
        crc=0x8D9C08B1,
    ),
)

ALL_HEADERS_TYPES = (
    (
        b"\x00\x00\x00\x62\x00\x00\x00\x52\x03\xb5\xcb\x9c"
        b"\x010\x00\x011\x01\x012\x02\x02\x013\x03\x00\x03"
        b"\x014\x04\x00\x00\x00\x04\x015\x05\x00\x00\x00\x00\x00\x00\x00\x05"
        b"\x016\x06\x00\x05bytes\x017\x07\x00\x04utf8"
        b"\x018\x08\x00\x00\x00\x00\x00\x00\x00\x08\x019\x090123456789abcdef"
        b"\x63\x35\x36\x71"
    ),
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x62,
            headers_length=0x52,
            crc=0x03B5CB9C,
        ),
        headers={
            '0': True,
            '1': False,
            '2': 0x02,
            '3': 0x03,
            '4': 0x04,
            '5': 0x05,
            '6': b'bytes',
            '7': 'utf8',
            '8': 0x08,
            '9': b'0123456789abcdef',
        },
        payload=b"",
        crc=0x63353671,
    ),
)

ERROR_EVENT_MESSAGE = (
    (
        b"\x00\x00\x00\x52\x00\x00\x00\x42\xbf\x23\x63\x7e"
        b"\x0d:message-type\x07\x00\x05error"
        b"\x0b:error-code\x07\x00\x04code"
        b"\x0e:error-message\x07\x00\x07message"
        b"\x6b\x6c\xea\x3d"
    ),
    EventStreamMessage(
        prelude=MessagePrelude(
            total_length=0x52,
            headers_length=0x42,
            crc=0xBF23637E,
        ),
        headers={
            ':message-type': 'error',
            ':error-code': 'code',
            ':error-message': 'message',
        },
        payload=b'',
        crc=0x6B6CEA3D,
    ),
)

# Tuples of encoded messages and their expected decoded output
POSITIVE_CASES = [
    EMPTY_MESSAGE,
    INT8_HEADER,
    INT16_HEADER,
    INT32_HEADER,
    INT64_HEADER,
    PAYLOAD_NO_HEADERS,
    PAYLOAD_ONE_STR_HEADER,
    ALL_HEADERS_TYPES,
    ERROR_EVENT_MESSAGE,
]

CORRUPTED_HEADER_LENGTH = (
    (
        b"\x00\x00\x00=\xff\x00\x01\x02\x07\xfd\x83\x96\x0ccontent-type\x07\x00"
        b"\x10application/json{'foo':'bar'}\x8d\x9c\x08\xb1"
    ),
    ChecksumMismatch,
)

CORRUPTED_HEADERS = (
    (
        b"\x00\x00\x00=\x00\x00\x00 \x07\xfd\x83\x96\x0ccontent+type\x07\x00\x10"
        b"application/json{'foo':'bar'}\x8d\x9c\x08\xb1"
    ),
    ChecksumMismatch,
)

CORRUPTED_LENGTH = (
    b"\x01\x00\x00\x1d\x00\x00\x00\x00\xfdR\x8cZ{'foo':'bar'}\xc3e96",
    ChecksumMismatch,
)

CORRUPTED_PAYLOAD = (
    b"\x00\x00\x00\x1d\x00\x00\x00\x00\xfdR\x8cZ{'foo':'bar'\x8d\xc3e96",
    ChecksumMismatch,
)

DUPLICATE_HEADER = (
    (
        b"\x00\x00\x00\x24\x00\x00\x00\x14\x4b\xb9\x82\xd0"
        b"\x04test\x04asdf\x04test\x04asdf\xf3\xf4\x75\x63"
    ),
    DuplicateHeader,
)

# In contrast to the CORRUPTED_HEADERS case, this message is otherwise
# well-formed - the checksums match.
INVALID_HEADERS_LENGTH = (
    (
        b"\x00\x00\x00\x3d"  # total length
        b"\xff\x00\x01\x02"  # headers length
        b"\x15\x83\xf5\xc2"  # prelude crc
        b"\x0ccontent-type\x07\x00\x10application/json"  # headers
        b"{'foo':'bar'}"  # payload
        b"\x2f\x37\x7f\x5d"  # message crc
    ),
    InvalidHeadersLength,
)

# In contrast to the CORRUPTED_PAYLOAD case, this message is otherwise
# well-formed - the checksums match.
INVALID_PAYLOAD_LENGTH = (
    b"\x01\x80\x00\x21"  # total length
    + b"\x00\x00\x00\x00"  # headers length
    + b"\xdd\x3f\x33\xb1"  # prelude crc
    + b"0" * (24 * 1024**2 + 1)  # payload
    + b"\x2a\xb4\xc5\xa5",  # message crc
    InvalidPayloadLength,
)


# Tuples of encoded messages and their expected exception
NEGATIVE_CASES = [
    CORRUPTED_LENGTH,
    CORRUPTED_PAYLOAD,
    CORRUPTED_HEADERS,
    CORRUPTED_HEADER_LENGTH,
    DUPLICATE_HEADER,
    INVALID_HEADERS_LENGTH,
    INVALID_PAYLOAD_LENGTH,
]


def assert_message_equal(message_a, message_b):
    """Asserts all fields for two messages are equal."""
    assert message_a.prelude.total_length == message_b.prelude.total_length
    assert message_a.prelude.headers_length == message_b.prelude.headers_length
    assert message_a.prelude.crc == message_b.prelude.crc
    assert message_a.headers == message_b.headers
    assert message_a.payload == message_b.payload
    assert message_a.crc == message_b.crc


def test_partial_message():
    """Ensure that we can receive partial payloads."""
    data = EMPTY_MESSAGE[0]
    event_buffer = EventStreamBuffer()
    # This mid point is an arbitrary break in the middle of the headers
    mid_point = 15
    event_buffer.add_data(data[:mid_point])
    messages = list(event_buffer)
    assert messages == []
    event_buffer.add_data(data[mid_point : len(data)])
    for message in event_buffer:
        assert_message_equal(message, EMPTY_MESSAGE[1])


def check_message_decodes(encoded, decoded):
    """Ensure the message decodes to what we expect."""
    event_buffer = EventStreamBuffer()
    event_buffer.add_data(encoded)
    messages = list(event_buffer)
    assert len(messages) == 1
    assert_message_equal(messages[0], decoded)


@pytest.mark.parametrize("encoded, decoded", POSITIVE_CASES)
def test_positive_cases(encoded, decoded):
    """Test that all positive cases decode how we expect."""
    check_message_decodes(encoded, decoded)


def test_all_positive_cases():
    """Test all positive cases can be decoded on the same buffer."""
    event_buffer = EventStreamBuffer()
    # add all positive test cases to the same buffer
    for encoded, _ in POSITIVE_CASES:
        event_buffer.add_data(encoded)
    # collect all of the expected messages
    expected_messages = [decoded for (_, decoded) in POSITIVE_CASES]
    # collect all of the decoded messages
    decoded_messages = list(event_buffer)
    # assert all messages match what we expect
    for expected, decoded in zip(expected_messages, decoded_messages):
        assert_message_equal(expected, decoded)


@pytest.mark.parametrize(
    "encoded, exception",
    NEGATIVE_CASES,
    ids=[
        "corrupted-length",
        "corrupted-payload",
        "corrupted-headers",
        "corrupted-headers-length",
        "duplicate-headers",
        "invalid-headers-length",
        "invalid-payload-length",
    ],
)
def test_negative_cases(encoded, exception):
    """Test that all negative cases raise the expected exception."""
    with pytest.raises(exception):
        check_message_decodes(encoded, None)


def test_header_parser():
    """Test that the header parser supports all header types."""
    headers_data = (
        b"\x010\x00\x011\x01\x012\x02\x02\x013\x03\x00\x03"
        b"\x014\x04\x00\x00\x00\x04\x015\x05\x00\x00\x00\x00\x00\x00\x00\x05"
        b"\x016\x06\x00\x05bytes\x017\x07\x00\x04utf8"
        b"\x018\x08\x00\x00\x00\x00\x00\x00\x00\x08\x019\x090123456789abcdef"
    )

    expected_headers = {
        '0': True,
        '1': False,
        '2': 0x02,
        '3': 0x03,
        '4': 0x04,
        '5': 0x05,
        '6': b'bytes',
        '7': 'utf8',
        '8': 0x08,
        '9': b'0123456789abcdef',
    }

    parser = EventStreamHeaderParser()
    headers = parser.parse(headers_data)
    assert headers == expected_headers


def test_message_prelude_properties():
    """Test that calculated properties from the payload are correct."""
    # Total length: 40, Headers Length: 15, random crc
    prelude = MessagePrelude(40, 15, 0x00000000)
    assert prelude.payload_length == 9
    assert prelude.headers_end == 27
    assert prelude.payload_end == 36


def test_message_to_response_dict():
    response_dict = PAYLOAD_ONE_STR_HEADER[1].to_response_dict()
    assert response_dict['status_code'] == 200

    expected_headers = {'content-type': 'application/json'}
    assert response_dict['headers'] == expected_headers
    assert response_dict['body'] == b"{'foo':'bar'}"


def test_message_to_response_dict_error():
    response_dict = ERROR_EVENT_MESSAGE[1].to_response_dict()
    assert response_dict['status_code'] == 400
    headers = {
        ':message-type': 'error',
        ':error-code': 'code',
        ':error-message': 'message',
    }
    assert response_dict['headers'] == headers
    assert response_dict['body'] == b''


def test_unpack_uint8():
    (value, bytes_consumed) = DecodeUtils.unpack_uint8(b'\xde')
    assert bytes_consumed == 1
    assert value == 0xDE


def test_unpack_uint32():
    (value, bytes_consumed) = DecodeUtils.unpack_uint32(b'\xde\xad\xbe\xef')
    assert bytes_consumed == 4
    assert value == 0xDEADBEEF


def test_unpack_int8():
    (value, bytes_consumed) = DecodeUtils.unpack_int8(b'\xfe')
    assert bytes_consumed == 1
    assert value == -2


def test_unpack_int16():
    (value, bytes_consumed) = DecodeUtils.unpack_int16(b'\xff\xfe')
    assert bytes_consumed == 2
    assert value == -2


def test_unpack_int32():
    (value, bytes_consumed) = DecodeUtils.unpack_int32(b'\xff\xff\xff\xfe')
    assert bytes_consumed == 4
    assert value == -2


def test_unpack_int64():
    test_bytes = b'\xff\xff\xff\xff\xff\xff\xff\xfe'
    (value, bytes_consumed) = DecodeUtils.unpack_int64(test_bytes)
    assert bytes_consumed == 8
    assert value == -2


def test_unpack_array_short():
    test_bytes = b'\x00\x10application/json'
    (value, bytes_consumed) = DecodeUtils.unpack_byte_array(test_bytes)
    assert bytes_consumed == 18
    assert value == b'application/json'


def test_unpack_byte_array_int():
    (value, array_bytes_consumed) = DecodeUtils.unpack_byte_array(
        b'\x00\x00\x00\x10application/json', length_byte_size=4
    )
    assert array_bytes_consumed == 20
    assert value == b'application/json'


def test_unpack_utf8_string():
    length = b'\x00\x09'
    utf8_string = b'\xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e'
    encoded = length + utf8_string
    (value, bytes_consumed) = DecodeUtils.unpack_utf8_string(encoded)
    assert bytes_consumed == 11
    assert value == utf8_string.decode('utf-8')


def test_unpack_prelude():
    data = b'\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x03'
    prelude = DecodeUtils.unpack_prelude(data)
    assert prelude == ((1, 2, 3), 12)


def create_mock_raw_stream(*data):
    raw_stream = mock.Mock()

    def generator():
        yield from data

    raw_stream.stream = generator
    return raw_stream


_PRELUDE_LENGTH = 12


def encode_event_stream_message(headers, payload):
    """Encodes headers and a payload into an event stream message.

    Only string valued headers are supported as that is all that is
    needed to exercise the event stream wrapper.
    """
    header_bytes = b''
    for name, value in headers.items():
        encoded_name = name.encode('utf-8')
        encoded_value = value.encode('utf-8')
        header_bytes += bytes([len(encoded_name)]) + encoded_name
        # A type of 7 indicates a utf-8 string value
        header_bytes += b'\x07' + len(encoded_value).to_bytes(2, 'big')
        header_bytes += encoded_value
    total_length = _PRELUDE_LENGTH + len(header_bytes) + len(payload) + 4
    prelude = total_length.to_bytes(4, 'big') + len(header_bytes).to_bytes(
        4, 'big'
    )
    message = prelude + (crc32(prelude) & 0xFFFFFFFF).to_bytes(4, 'big')
    message += header_bytes + payload
    message += (crc32(message) & 0xFFFFFFFF).to_bytes(4, 'big')
    return message


def create_event_message(event_type, payload=b''):
    return encode_event_stream_message(
        {
            ':message-type': 'event',
            ':event-type': event_type,
            ':content-type': 'application/json',
        },
        payload,
    )


def test_event_stream_wrapper_iteration():
    raw_stream = create_mock_raw_stream(
        b"\x00\x00\x00+\x00\x00\x00\x0e4\x8b\xec{\x08event-id\x04\x00",
        b"\x00\xa0\x0c{'foo':'bar'}\xd3\x89\x02\x85",
    )
    parser = mock.Mock(spec=EventStreamXMLParser)
    output_shape = mock.Mock()
    event_stream = EventStream(raw_stream, output_shape, parser, '')
    events = list(event_stream)
    assert len(events) == 1

    response_dict = {
        'headers': {'event-id': 0x0000A00C},
        'body': b"{'foo':'bar'}",
        'status_code': 200,
    }
    parser.parse.assert_called_with(response_dict, output_shape)


def test_eventstream_wrapper_iteration_error():
    raw_stream = create_mock_raw_stream(ERROR_EVENT_MESSAGE[0])
    parser = mock.Mock(spec=EventStreamXMLParser)
    parser.parse.return_value = {}
    output_shape = mock.Mock()
    event_stream = EventStream(raw_stream, output_shape, parser, '')
    with pytest.raises(EventStreamError):
        list(event_stream)


def test_event_stream_wrapper_close():
    raw_stream = mock.Mock()
    event_stream = EventStream(raw_stream, None, None, '')
    event_stream.close()
    raw_stream.close.assert_called_once_with()


def test_event_stream_initial_response():
    raw_stream = create_mock_raw_stream(
        b'\x00\x00\x00~\x00\x00\x00O\xc5\xa3\xdd\xc6\r:message-type\x07\x00',
        b'\x05event\x0b:event-type\x07\x00\x10initial-response\r:content-type',
        b'\x07\x00\ttext/json{"InitialResponse": "sometext"}\xf6\x98$\x83',
    )
    parser = mock.Mock(spec=EventStreamXMLParser)
    output_shape = mock.Mock()
    event_stream = EventStream(raw_stream, output_shape, parser, '')
    event = event_stream.get_initial_response()
    headers = {
        ':message-type': 'event',
        ':event-type': 'initial-response',
        ':content-type': 'text/json',
    }
    payload = b'{"InitialResponse": "sometext"}'
    assert event.headers == headers
    assert event.payload == payload


def test_event_stream_initial_response_wrong_type():
    raw_stream = create_mock_raw_stream(
        b"\x00\x00\x00+\x00\x00\x00\x0e4\x8b\xec{\x08event-id\x04\x00",
        b"\x00\xa0\x0c{'foo':'bar'}\xd3\x89\x02\x85",
    )
    parser = mock.Mock(spec=EventStreamXMLParser)
    output_shape = mock.Mock()
    event_stream = EventStream(raw_stream, output_shape, parser, '')
    with pytest.raises(NoInitialResponseError):
        event_stream.get_initial_response()


def test_event_stream_initial_response_no_event():
    raw_stream = create_mock_raw_stream(b'')
    parser = mock.Mock(spec=EventStreamXMLParser)
    output_shape = mock.Mock()
    event_stream = EventStream(raw_stream, output_shape, parser, '')
    with pytest.raises(NoInitialResponseError):
        event_stream.get_initial_response()


def test_event_stream_unknown_event_skipped_by_default():
    unknown_message = create_event_message('UnknownEvent', b'{"foo": "bar"}')
    raw_stream = create_mock_raw_stream(unknown_message)
    parser = mock.Mock(spec=EventStreamXMLParser)
    # A real parser produces an empty dict for unmodeled event types
    parser.parse.return_value = {}
    output_shape = mock.Mock()
    output_shape.members = {}
    event_stream = EventStream(raw_stream, output_shape, parser, '')
    assert list(event_stream) == []


def test_event_stream_unknown_event_passthrough():
    unknown_message = create_event_message('UnknownEvent', b'{"foo": "bar"}')
    # Feed the message to the stream one byte at a time to ensure
    # chunking does not affect the passthrough behavior
    raw_stream = create_mock_raw_stream(
        *[unknown_message[i : i + 1] for i in range(len(unknown_message))]
    )
    parser = mock.Mock(spec=EventStreamXMLParser)
    output_shape = mock.Mock()
    output_shape.members = {}
    event_stream = EventStream(
        raw_stream, output_shape, parser, '', include_unknown_events=True
    )
    events = list(event_stream)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, UnknownEventStreamEvent)
    assert event.event_type == 'UnknownEvent'
    assert event.headers == {
        ':message-type': 'event',
        ':event-type': 'UnknownEvent',
        ':content-type': 'application/json',
    }
    assert event.payload == b'{"foo": "bar"}'
    parser.parse.assert_not_called()


def test_event_stream_unknown_event_between_known_events():
    known_message = create_event_message('KnownEvent', b'{"known": true}')
    unknown_message = create_event_message('UnknownEvent', b'{"foo": "bar"}')
    raw_stream = create_mock_raw_stream(
        known_message, unknown_message, known_message
    )
    parser = mock.Mock(spec=EventStreamXMLParser)
    parsed_known_event = {'KnownEvent': {'Payload': b'{"known": true}'}}
    parser.parse.return_value = parsed_known_event
    output_shape = mock.Mock()
    output_shape.members = {'KnownEvent': mock.Mock()}
    event_stream = EventStream(
        raw_stream, output_shape, parser, '', include_unknown_events=True
    )
    events = list(event_stream)
    assert len(events) == 3
    assert events[0] == parsed_known_event
    assert isinstance(events[1], UnknownEventStreamEvent)
    assert events[1].event_type == 'UnknownEvent'
    assert events[1].payload == b'{"foo": "bar"}'
    assert events[2] == parsed_known_event
    assert parser.parse.call_count == 2


def test_event_stream_unknown_event_corrupted_frame_raises():
    unknown_message = create_event_message('UnknownEvent', b'{"foo": "bar"}')
    # Corrupt a byte in the middle of the payload so the message crc
    # no longer matches
    corrupt_index = len(unknown_message) - 6
    corrupted = (
        unknown_message[:corrupt_index]
        + bytes([unknown_message[corrupt_index] ^ 0xFF])
        + unknown_message[corrupt_index + 1 :]
    )
    raw_stream = create_mock_raw_stream(corrupted)
    parser = mock.Mock(spec=EventStreamXMLParser)
    output_shape = mock.Mock()
    output_shape.members = {}
    event_stream = EventStream(
        raw_stream, output_shape, parser, '', include_unknown_events=True
    )
    with pytest.raises(ChecksumMismatch):
        list(event_stream)


@pytest.mark.parametrize(
    "headers",
    [
        {
            ':message-type': 'error',
            ':error-code': 'UnmodeledError',
            ':error-message': 'some message',
        },
        {
            ':message-type': 'exception',
            ':exception-type': 'UnmodeledException',
        },
    ],
    ids=['error', 'exception'],
)
def test_event_stream_error_messages_not_treated_as_unknown(headers):
    error_message = encode_event_stream_message(headers, b'')
    raw_stream = create_mock_raw_stream(error_message)
    parser = mock.Mock(spec=EventStreamXMLParser)
    parser.parse.return_value = {}
    output_shape = mock.Mock()
    output_shape.members = {}
    event_stream = EventStream(
        raw_stream, output_shape, parser, '', include_unknown_events=True
    )
    with pytest.raises(EventStreamError):
        list(event_stream)


def test_event_stream_known_event_parse_error_propagates():
    known_message = create_event_message('KnownEvent', b'{"known": true}')
    raw_stream = create_mock_raw_stream(known_message)
    parser = mock.Mock(spec=EventStreamXMLParser)
    parser.parse.side_effect = ResponseParserError(
        'Unable to parse known event'
    )
    output_shape = mock.Mock()
    output_shape.members = {'KnownEvent': mock.Mock()}
    event_stream = EventStream(
        raw_stream, output_shape, parser, '', include_unknown_events=True
    )
    with pytest.raises(ResponseParserError):
        list(event_stream)


def test_event_stream_unknown_event_underlying_body_closed_early():
    known_message = create_event_message('KnownEvent', b'{"known": true}')
    unknown_message = create_event_message('UnknownEvent', b'{"foo": "bar"}')
    # The underlying body is closed before the unknown event is fully
    # delivered, so only the complete known event is surfaced
    raw_stream = create_mock_raw_stream(
        known_message, unknown_message[: len(unknown_message) // 2]
    )
    parser = mock.Mock(spec=EventStreamXMLParser)
    parsed_known_event = {'KnownEvent': {'Payload': b'{"known": true}'}}
    parser.parse.return_value = parsed_known_event
    output_shape = mock.Mock()
    output_shape.members = {'KnownEvent': mock.Mock()}
    event_stream = EventStream(
        raw_stream, output_shape, parser, '', include_unknown_events=True
    )
    assert list(event_stream) == [parsed_known_event]
