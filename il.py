#!/usr/bin/env python3
"""
Tiny .NET IL browser for the extracted Xamarin DLLs (dnfile + dncil).

    python il.py <dll> list   <regex>     # list Type.Method matching regex
    python il.py <dll> disasm <regex>     # disassemble matching methods (IL + resolved tokens)

Token resolution turns raw metadata tokens into readable names/strings so the
byte-building of a command is legible without a full C# decompiler.
"""
from __future__ import annotations

import re
import sys

import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import CilMethodBodyReaderBase
from dncil.clr.token import StringToken, Token


class Reader(CilMethodBodyReaderBase):
    def __init__(self, pe: dnfile.dnPE, rva: int):
        self.pe = pe
        self.offset = pe.get_offset_from_rva(rva)

    def read(self, n: int) -> bytes:
        d = self.pe.get_data(self.pe.get_rva_from_offset(self.offset), n)
        self.offset += n
        return d

    def tell(self) -> int:
        return self.offset

    def seek(self, o: int) -> int:
        self.offset = o
        return o


def build_index(pe: dnfile.dnPE):
    """Return list of (fullname, method_row), nested types shown as Outer+Inner."""
    types = pe.net.mdtables.TypeDef.rows
    enclosing = {}
    try:
        for nc in pe.net.mdtables.NestedClass.rows:
            enclosing[nc.NestedClass.row_index] = nc.EnclosingClass.row_index
    except Exception:
        pass

    def fullname(i: int) -> str:  # i = 1-based TypeDef row_index
        t = types[i - 1]
        tn = t.TypeName or ""
        if i in enclosing:
            return f"{fullname(enclosing[i])}+{tn}"
        ns = t.TypeNamespace or ""
        return f"{ns}.{tn}" if ns else tn

    out = []
    for i, t in enumerate(types, start=1):
        fn = fullname(i)
        for mi in t.MethodList:
            out.append((f"{fn}::{mi.row.Name}", mi.row))
    return out


def resolve(pe: dnfile.dnPE, tok) -> str:
    md = pe.net.mdtables
    try:
        if isinstance(tok, StringToken):
            return '"' + str(pe.net.user_strings.get(tok.rid).value) + '"'
    except Exception:
        return "<str?>"
    try:
        table = tok.table if hasattr(tok, "table") else (tok.value >> 24)
        rid = tok.rid if hasattr(tok, "rid") else (tok.value & 0xFFFFFF)
        if table == 6:   # MethodDef
            return md.MethodDef.rows[rid - 1].Name
        if table == 10:  # MemberRef
            return md.MemberRef.rows[rid - 1].Name
        if table == 4:   # Field
            return md.Field.rows[rid - 1].Name
        if table == 1:   # TypeRef
            return "T:" + md.TypeRef.rows[rid - 1].TypeName
        if table == 2:   # TypeDef
            return "T:" + md.TypeDef.rows[rid - 1].TypeName
    except Exception:
        pass
    return str(tok)


def disasm(pe: dnfile.dnPE, row) -> None:
    if not row.Rva:
        print("    <no body>")
        return
    try:
        body = CilMethodBody(Reader(pe, row.Rva))
    except Exception as e:  # noqa: BLE001
        print(f"    <disasm failed: {e}>")
        return
    for insn in body.instructions:
        op = ""
        if insn.operand is not None:
            if isinstance(insn.operand, (Token, StringToken)):
                op = resolve(pe, insn.operand)
            else:
                op = str(insn.operand)
        print(f"    IL_{insn.offset:04x}  {insn.mnemonic:<12} {op}")


def main() -> None:
    dll, mode, pattern = sys.argv[1], sys.argv[2], sys.argv[3]
    pe = dnfile.dnPE(dll)
    rx = re.compile(pattern, re.I)
    idx = build_index(pe)
    hits = [(name, row) for name, row in idx if rx.search(name)]
    print(f"[{len(hits)} match(es) for /{pattern}/]\n")
    for name, row in hits:
        print(f"== {name}")
        if mode == "disasm":
            disasm(pe, row)
            print()


if __name__ == "__main__":
    main()
