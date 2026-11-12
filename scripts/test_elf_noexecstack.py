# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure synthetic bytes only: never an executable program or native tool."""
import pathlib
import struct
import tempfile
import unittest
from unittest import mock

import elf_stack_policy as elf


def elf_image(stack_flags=6, load_flags=5, stacks=1, loads=1):
    count = stacks + loads
    total = elf.HEADER.size + count * elf.PROGRAM.size + 16
    ident = b'\x7fELF\x02\x01\x01' + b'\x00' * 9
    header = elf.HEADER.pack(ident, 2, 62, 1, 0, 64, 0, 0, 64, 56, count, 0, 0, 0)
    programs = [elf.PROGRAM.pack(1, load_flags, 0, 0, 0, total, total, 4096)] * loads
    programs += [elf.PROGRAM.pack(elf.PT_GNU_STACK, stack_flags, 0, 0, 0, 0, 0, 16)] * stacks
    return header + b''.join(programs) + b'fixture-not-code'


class ELFNoexecstackTest(unittest.TestCase):
    def test_valid_and_observation_only_baseline(self):
        for stack in (0, 2, 4, 6):
            info = elf.parse_elf(elf_image(stack_flags=stack))
            elf.check_policy(info)
            self.assertEqual(info['gnu_stack_flags'], stack)
        for stack in (6, 7):
            info = elf.parse_elf(elf_image(stack_flags=stack))
            self.assertEqual(info['gnu_stack_flags'], stack)

    def test_execute_stack_and_wx_load_rejected(self):
        for stack, load, error in [(7, 5, 'GNU_STACK'), (6, 7, 'LOAD'), (5, 5, 'GNU_STACK')]:
            with self.subTest(stack=stack, load=load):
                with self.assertRaisesRegex(elf.ELFError, error):
                    elf.check_policy(elf.parse_elf(elf_image(stack, load)))

    def test_missing_duplicate_and_unknown_permissions(self):
        for options in ({'stacks': 0}, {'stacks': 2}, {'loads': 0}, {'stack_flags': 14}, {'load_flags': 13}):
            with self.subTest(options=options), self.assertRaises(elf.ELFError):
                elf.parse_elf(elf_image(**options))

    def test_unsupported_header_and_table_shapes(self):
        changes = [(0, b'NOPE'), (4, b'\x01'), (5, b'\x02'), (6, b'\x00'),
                   (7, b'\x09'), (16, struct.pack('<H', 1)), (18, struct.pack('<H', 183)),
                   (20, struct.pack('<I', 2)), (32, struct.pack('<Q', 10**9)),
                   (32, struct.pack('<Q', 0)), (52, struct.pack('<H', 0)),
                   (54, struct.pack('<H', 0)), (56, struct.pack('<H', 0)),
                   (56, struct.pack('<H', 65535))]
        for offset, value in changes:
            data = bytearray(elf_image()); data[offset:offset + len(value)] = value
            with self.subTest(offset=offset, value=value), self.assertRaises(elf.ELFError):
                elf.parse_elf(data)

    def test_truncation_extents_and_memory_size(self):
        data = elf_image()
        for length in (0, 16, 63, 64, 119, len(data) - 1):
            with self.subTest(length=length), self.assertRaises(elf.ELFError):
                elf.parse_elf(data[:length])
        for offset, value in ((64 + 8, 10**9), (64 + 32, 10**9), (64 + 40, 0)):
            changed = bytearray(data); struct.pack_into('<Q', changed, offset, value)
            with self.subTest(offset=offset), self.assertRaises(elf.ELFError):
                elf.parse_elf(changed)
        with mock.patch.object(elf, 'MAX_BYTES', 100), self.assertRaises(elf.ELFError):
            elf.parse_elf(data)

    def test_regular_file_hash_identity_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'input'
            path.write_bytes(elf_image())
            info = elf.inspect_file(path)
            self.assertEqual(info['bytes'], len(elf_image()))
            self.assertEqual(len(info['sha256']), 64)
            self.assertTrue(info['policy_enforced'])
            link = path.with_name('link'); link.symlink_to(path)
            with self.assertRaises(elf.ELFError): elf.inspect_file(link)
            with self.assertRaises(elf.ELFError): elf.inspect_file(path.parent)
            path.write_bytes(elf_image(stack_flags=7))
            self.assertEqual(elf.inspect_file(path, enforce=False)['gnu_stack_flags'], 7)
            with self.assertRaises(elf.ELFError): elf.inspect_file(path)


if __name__ == '__main__':
    unittest.main()
