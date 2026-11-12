# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded ELF64/LSB/x86-64 program-header inspection; never execute the input."""
import argparse
import hashlib
import json
import os
import pathlib
import stat
import struct

MAX_BYTES = 256 * 1024 * 1024
HEADER = struct.Struct('<16sHHIQQQIHHHHHH')
PROGRAM = struct.Struct('<IIQQQQQQ')
PT_LOAD = 1
PT_GNU_STACK = 0x6474E551


class ELFError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ELFError(reason)


def parse_elf(data):
    require(HEADER.size <= len(data) <= MAX_BYTES, 'invalid or truncated ELF size')
    ident, kind, machine, version, entry, phoff, shoff, flags, ehsize, phsize, count, shsize, shcount, shnames = HEADER.unpack_from(data)
    require(ident[:4] == b'\x7fELF', 'not ELF')
    require(ident[4:7] == b'\x02\x01\x01', 'requires ELF64 little-endian version 1')
    require(ident[7] in (0, 3), 'unsupported ELF ABI')
    require(kind in (2, 3) and machine == 62 and version == 1, 'requires x86-64 EXEC/DYN version 1')
    require(ehsize == HEADER.size and phsize == PROGRAM.size, 'unexpected header sizes')
    require(1 <= count <= 128 and phoff >= HEADER.size, 'missing/extended/unsupported program headers')
    require(phoff + count * phsize <= len(data), 'truncated program-header table')
    programs = []
    for index in range(count):
        ptype, pflags, offset, vaddr, paddr, filesz, memsz, align = PROGRAM.unpack_from(data, phoff + index * phsize)
        require(offset <= len(data) and filesz <= len(data) - offset, 'program file extent exceeds input')
        if ptype in (PT_LOAD, PT_GNU_STACK):
            require(pflags & ~7 == 0, 'unknown LOAD/GNU_STACK permission bits')
        if ptype == PT_LOAD:
            require(filesz <= memsz, 'LOAD file size exceeds memory size')
        programs.append({'type': ptype, 'flags': pflags, 'offset': offset,
                         'file_bytes': filesz, 'memory_bytes': memsz})
    loads = [p for p in programs if p['type'] == PT_LOAD]
    stacks = [p for p in programs if p['type'] == PT_GNU_STACK]
    require(loads, 'missing LOAD segment')
    require(len(stacks) == 1, 'requires exactly one GNU_STACK segment')
    return {'elf_class': 64, 'byte_order': 'little', 'machine': machine,
            'elf_type': kind, 'program_header_count': count,
            'gnu_stack_flags': stacks[0]['flags'], 'load_flags': [p['flags'] for p in loads]}


def check_policy(info):
    require(not info['gnu_stack_flags'] & 1, 'GNU_STACK has execute permission')
    require(all(flags & 3 != 3 for flags in info['load_flags']), 'LOAD has write and execute permissions')


def identity(st):
    return [st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def read_regular(path):
    path = pathlib.Path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), 'input is not a regular non-symlink file')
    require(0 <= before.st_size <= MAX_BYTES, 'input exceeds size limit')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        require(identity(os.fstat(stream.fileno())) == identity(before), 'input changed while opening')
        data = stream.read(MAX_BYTES + 1)
        require(len(data) == before.st_size, 'input size changed while reading')
        require(identity(os.fstat(stream.fileno())) == identity(before), 'open input changed while reading')
    require(identity(path.lstat()) == identity(before), 'input path changed while reading')
    return data, identity(before)


def inspect_file(path, enforce=True):
    data, stamp = read_regular(path)
    info = parse_elf(data)
    if enforce:
        check_policy(info)
    return {'path': str(path), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'identity': stamp, 'policy_enforced': enforce, **info}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe', action='store_true', help='record permissions without enforcing non-executable policy')
    parser.add_argument('binary')
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_file(args.binary, enforce=not args.observe), sort_keys=True))
    except (OSError, ELFError) as error:
        parser.exit(1, f'ERROR: incomplete or rejected ELF inspection: {error}\n')


if __name__ == '__main__':
    main()
