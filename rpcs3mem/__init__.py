"""rpcs3mem - read and write the guest memory of a running RPCS3.

Three interchangeable sources:

* ProcessMemory - the rpcs3 process itself (fast; works next to KAMI)
* PineMemory    - RPCS3's IPC socket (PINE), the interface KAMI uses
* DumpMemory    - a folder written by Utilities > Dump Guest Memory
"""
from .common import MemoryError_, Region, parse_int, parse_range  # noqa: F401
from .backend import (PineMemory, add_backend_args, open_backend, pine_info, read_f32,  # noqa: F401
                      read_u32, resolve_chain, write_f32, write_u32)
from .pine import PineBusy, PineClient, PineError, PineFail  # noqa: F401

__version__ = "0.1.0"
