#!/usr/bin/env python
"""调试工具：dump 指定 PDF 指定页的文本/表格/区块。

用法：
    python -X utf8 scripts/inspect_pdf.py raw/pdf/600249/xxx.pdf 6       # 第6页
    python -X utf8 scripts/inspect_pdf.py raw/pdf/600249/xxx.pdf 6 10    # 第6-10页
"""
import sys

import pymupdf as fitz


def main():
    pdf_path, page_spec = sys.argv[1], sys.argv[2]
    end = sys.argv[3] if len(sys.argv) > 3 else None
    doc = fitz.open(pdf_path)
    start = int(page_spec)
    stop = int(end) if end else start
    for pno in range(start, min(stop, doc.page_count) + 1):
        page = doc[pno - 1]
        print(f"\n{'=' * 20} PAGE {pno} {'=' * 20}")
        print("--- blocks:")
        for b in sorted(page.get_text("blocks"), key=lambda b: (round(b[1], 1), b[0])):
            print(f"[{b[0]:.0f},{b[1]:.0f},{b[2]:.0f},{b[3]:.0f}] {b[6]} | {(b[4] or '').strip()[:100]}")
        print("--- tables:")
        for t in page.find_tables().tables:
            for row in t.extract():
                print(" | ".join(str(c or "").replace("\n", " ")[:40] for c in row))
    doc.close()


if __name__ == "__main__":
    main()
