"""Import allow list for the Windows native library: every DLL a built libmpv-2.dll imports must be a
Windows system component. Standard library only; it reads the PE import table and changes nothing.

    pe_imports.py <dll>

Prints the imported DLL names that are not allowed, one per line, and exits 1 when there are any. A DLL that
is not a PE file, or whose import table cannot be read, is refused, so a truncated build cannot pass.
"""
import re
import struct
import sys
from pathlib import Path

# Windows system components a MinGW-built libmpv with Schannel and no bundled runtime imports. API-set stubs
# (api-ms-win-*, ext-ms-*) resolve to system components too.
ALLOWED = re.compile(
    r'(?i)^(?:ADVAPI32|AVRT|bcrypt|CRYPT32|dwmapi|GDI32|IMM32|KERNEL32|msvcrt|ncrypt|ntdll|ole32|OPENGL32|'
    r'Secur32|SHCORE|SHELL32|SHLWAPI|USER32|UxTheme|WS2_32)\.dll$|^(?:api-ms-win|ext-ms)-[a-z0-9-]+\.dll$')


def import_names(path):
    """The DLL names in the PE import directory, in file order."""
    data = Path(path).read_bytes()
    if data[:2] != b'MZ':
        raise ValueError('not a PE file')
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('missing PE signature')
    section_count, = struct.unpack_from('<H', data, pe + 6)
    optional = pe + 24
    magic, = struct.unpack_from('<H', data, optional)
    if magic not in (0x10B, 0x20B):
        raise ValueError('unknown optional header')
    directories = optional + (112 if magic == 0x20B else 96)
    import_rva, import_size = struct.unpack_from('<II', data, directories + 8)
    if not import_rva:
        return []
    table = optional + struct.unpack_from('<H', data, pe + 20)[0]
    sections = []
    for index in range(section_count):
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from('<IIII', data, table + index * 40 + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_pointer))

    def offset(rva):
        for address, size, pointer in sections:
            if address <= rva < address + size:
                return pointer + rva - address
        raise ValueError(f'address {rva:#x} is outside every section')

    names = []
    position = offset(import_rva)
    while True:
        _, _, _, name_rva, first_thunk = struct.unpack_from('<IIIII', data, position)
        if name_rva == 0 and first_thunk == 0:
            break
        start = offset(name_rva)
        names.append(data[start:data.index(b'\0', start)].decode('ascii'))
        position += 20
    return names


def unexpected(path):
    """The imported DLL names the allow list refuses."""
    return [name for name in import_names(path) if not ALLOWED.match(name)]


def main(argv):
    if len(argv) != 2:
        print('usage: pe_imports.py <dll>', file=sys.stderr)
        return 2
    try:
        names = import_names(argv[1])
        refused = unexpected(argv[1])
    except (OSError, ValueError, struct.error) as exc:
        print(f'The native library imports could not be read: {exc}', file=sys.stderr)
        return 1
    if refused:
        for name in refused:
            print(name)
        return 1
    print(f'OK   Import allow list: {len(names)} DLLs imported, all system components.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
