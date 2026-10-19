"""Private binary-pipe worker isolating upstream native panics from the phone app.

The only input/output is mono float32 PCM at 48 kHz. No files are written and no
network tools or API keys are needed. One worker/state belongs to one call.
"""

import argparse
import ctypes
import json
import struct
import sys
from array import array

from dots_phone.deepfilter_assets import HOP_SAMPLES, SAMPLE_RATE, verify_model


def exact(stream, length):
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            raise EOFError()
        data.extend(chunk)
    return bytes(data)


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--library", required=True, type=Path)
    args = parser.parse_args()
    if sys.byteorder != "little":
        raise RuntimeError("DeepFilterNet worker requires a little-endian runtime")
    verify_model(args.model)
    library = ctypes.CDLL(str(args.library))
    pointer = ctypes.POINTER(ctypes.c_float)
    library.df_create.argtypes = [ctypes.c_char_p, ctypes.c_float]
    library.df_create.restype = ctypes.c_void_p
    library.df_get_frame_length.argtypes = [ctypes.c_void_p]
    library.df_get_frame_length.restype = ctypes.c_size_t
    library.df_set_post_filter_beta.argtypes = [ctypes.c_void_p, ctypes.c_float]
    library.df_set_post_filter_beta.restype = None
    library.df_process_frame.argtypes = [ctypes.c_void_p, pointer, pointer]
    library.df_process_frame.restype = ctypes.c_float
    library.df_free.argtypes = [ctypes.c_void_p]
    library.df_free.restype = None
    state = library.df_create(str(args.model).encode(), 100.0)
    if not state:
        raise RuntimeError("DeepFilterNet state unavailable")
    try:
        if library.df_get_frame_length(state) != HOP_SAMPLES:
            raise RuntimeError("Unexpected DeepFilterNet model hop")
        library.df_set_post_filter_beta(state, 0.0)
        print(
            json.dumps({"ready": True, "sample_rate": SAMPLE_RATE, "hop": HOP_SAMPLES}), flush=True
        )
        source, target = sys.stdin.buffer, sys.stdout.buffer
        while True:
            size = struct.unpack("<I", exact(source, 4))[0]
            if not size or size % (4 * HOP_SAMPLES) or size > 4 * HOP_SAMPLES * 50:
                raise RuntimeError("Invalid DeepFilterNet packet")
            incoming = array("f")
            incoming.frombytes(exact(source, size))
            outgoing = array("f", [0.0]) * len(incoming)
            for offset in range(0, len(incoming), HOP_SAMPLES):
                library.df_process_frame(
                    state,
                    ctypes.cast(incoming.buffer_info()[0] + offset * 4, pointer),
                    ctypes.cast(outgoing.buffer_info()[0] + offset * 4, pointer),
                )
            target.write(outgoing.tobytes())
            target.flush()
    except EOFError:
        pass
    finally:
        library.df_free(state)


if __name__ == "__main__":
    main()
