import logging
import re
import struct

from pypsrp._utils import to_bytes
from pypsrp.exceptions import WinRMError

log = logging.getLogger(__name__)


class WinRMEncryption(object):

    SIXTEEN_KB = 16384
    MIME_BOUNDARY = "--Encrypted Boundary"
    CREDSSP = "application/HTTP-CredSSP-session-encrypted"
    SPNEGO = "application/HTTP-SPNEGO-session-encrypted"

    def __init__(self, auth, protocol):
        log.debug("Initialising WinRMEncryption helper for protocol %s"
                  % protocol)
        self.auth = auth
        self.protocol = protocol

        if protocol == self.SPNEGO:
            self._wrap = self._wrap_spnego
            self._unwrap = self._unwrap_spnego
        else:
            self._wrap = self._wrap_credssp
            self._unwrap = self._unwrap_credssp

    def wrap_message(self, message, hostname):
        log.debug("Wrapping message for host: %s" % hostname)
        if self.protocol == self.CREDSSP and len(message) > self.SIXTEEN_KB:
            content_type = "multipart/x-multi-encrypted"
            encrypted_msg = b""
            chunks = [message[i:i + self.SIXTEEN_KB] for i in
                      range(0, len(message), self.SIXTEEN_KB)]
            for chunk in chunks:
                encrypted_chunk = self._wrap_message(chunk, hostname)
                encrypted_msg += encrypted_chunk
        else:
            content_type = "multipart/encrypted"
            encrypted_msg = self._wrap_message(message, hostname)

        encrypted_msg += to_bytes("%s--\r\n" % self.MIME_BOUNDARY)

        log.debug("Created wrapped message of content type %s" % content_type)
        return content_type, encrypted_msg

    def unwrap_message(self, message, hostname):
        log.debug("Unwrapped message for host: %s" % hostname)
        parts = message.split(to_bytes("%s\r\n" % self.MIME_BOUNDARY))
        parts = list(filter(None, parts))

        message = b""
        for i in range(0, len(parts), 2):
            header = parts[i].strip()
            payload = parts[i + 1]

            expected_length = int(header.split(b"Length=")[1])

            # remove the end MIME block if it exists
            if payload.endswith(to_bytes("%s--\r\n" % self.MIME_BOUNDARY)):
                payload = payload[:len(payload) - 24]

            wrapped_data = payload.replace(
                b"\tContent-Type: application/octet-stream\r\n", b""
            )
            unwrapped_data = self._unwrap(wrapped_data, hostname)
            actual_length = len(unwrapped_data)

            log.debug("Actual unwrapped length: %d, expected unwrapped length:"
                      " %d" % (actual_length, expected_length))
            if actual_length != expected_length:
                raise WinRMError("The encrypted length from the server does "
                                 "not match the expected length, decryption "
                                 "failed, actual: %d != expected: %d"
                                 % (actual_length, expected_length))
            message += unwrapped_data

        return message

    def _wrap_message(self, message, hostname):
        msg_length = str(len(message))
        wrapped_data = self._wrap(message, hostname)

        payload = "\r\n".join([
            self.MIME_BOUNDARY,
            "\tContent-Type: %s" % self.protocol,
            "\tOriginalContent: type=application/soap+xml;charset=UTF-8;"
            "Length=%s" % msg_length,
            self.MIME_BOUNDARY,
            "\tContent-Type: application/octet-stream",
            ""
        ])
        payload = to_bytes(payload) + wrapped_data

        return payload

    def _wrap_spnego(self, data, hostname):
        context = self.auth.contexts[hostname]
        header, wrapped_data = context.wrap(data)

        return struct.pack("<i", len(header)) + header + wrapped_data

    def _wrap_credssp(self, data, hostname):
        context = self.auth.contexts[hostname]
        wrapped_data = context.wrap(data)
        cipher_negotiated = context.tls_connection.get_cipher_name()
        trailer_length = self._credssp_trailer(len(data), cipher_negotiated)

        return struct.pack("<i", trailer_length) + wrapped_data

    def _unwrap_spnego(self, data, hostname):
        context = self.auth.contexts[hostname]
        header_length = struct.unpack("<i", data[:4])[0]
        header = data[4:4 + header_length]
        wrapped_data = data[4 + header_length:]
        data = context.unwrap(header, wrapped_data)

        return data

    def _unwrap_credssp(self, data, hostname):
        context = self.auth.contexts[hostname]
        wrapped_data = data[4:]
        data = context.unwrap(wrapped_data)

        return data

    def _credssp_trailer(self, msg_len, cipher_suite):
        # On Windows this is derived from SecPkgContext_StreamSizes, this is
        # not available on other platforms so we need to calculate it manually
        log.debug("Attempting to get CredSSP trailer length for msg of "
                  "length %d with cipher %s" % (msg_len, cipher_suite))

        if re.match(r'^.*-GCM-[\w\d]*$', cipher_suite):
            # GCM has a fixed length of 16 bytes
            trailer_length = 16
        else:
            # For other cipher suites, trailer size == len(hmac) + len(padding)
            # the padding it the length required by the chosen block cipher
            hash_algorithm = cipher_suite.split('-')[-1]

            # while there are other algorithms, SChannel doesn't support them
            # as of yet so we just keep to this list
            hash_length = {
                'MD5': 16,
                'SHA': 20,
                'SHA256': 32,
                'SHA384': 48
            }.get(hash_algorithm, 0)

            pre_pad_length = msg_len + hash_length
            if "RC4" in cipher_suite:
                # RC4 is a stream cipher so no padding would be added
                padding_length = 0
            elif "DES" in cipher_suite or "3DES" in cipher_suite:
                # 3DES is a 64 bit block cipher
                padding_length = 8 - (pre_pad_length % 8)
            else:
                # AES is a 128 bit block cipher
                padding_length = 16 - (pre_pad_length % 16)

            trailer_length = (pre_pad_length + padding_length) - msg_len

        return trailer_length
