# Submission checklist — K4 L3B

## Thông tin bài nộp

| Trường | Giá trị |
| --- | --- |
| Học viên | Trần Quốc Vượng |
| Mã học viên | `2A202602522` |
| Lớp | `H209` |
| Variant | `l3b` |
| Output schema | `day09-l3b-output-v2` |
| Client | `tran-quoc-vuong-l3b-agent` `1.0.0` |

## Trước khi chạy batch

- [ ] `.env` có `COMPETITION_API_URL`, Team API Key `sk-team-...` và `MCP_ENDPOINT` thật.
- [x] `case-set.json` là `l3b-competition-v1`.
- [x] `inputs/` có đúng 100 file L3B.
- [ ] `day09 mcp-tools` xác thực và liệt kê được authoritative tools.
- [x] `pytest -q` và `ruff check src tests` pass.

## Chạy và xác thực

```powershell
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

- [ ] `outputs/` có đúng 100 file từ `L3B_CASE_001.json` đến `L3B_CASE_100.json`.
- [ ] `traces/trace.jsonl` đủ lifecycle và evidence linkage cho mọi case.
- [ ] Console báo `OK: 100 outputs / ... trace events`.
- [ ] Console báo `OK: .../dist/submission.zip`.

## Cấu trúc ZIP bắt buộc

```text
submission.zip
├── manifest.json
├── trace.jsonl
└── outputs/
    ├── L3B_CASE_001.json
    ├── ...
    └── L3B_CASE_100.json
```

- [ ] Không có thư mục bọc ngoài.
- [ ] Không có `src/`, `tests/`, `inputs/`, `.env`, API key hoặc debug log.
- [ ] Manifest dùng `variant_id: l3b` và `output_schema_version: day09-l3b-output-v2`.

## Trước commit và push

```powershell
git status --short
git diff --check
git diff --stat
git check-ignore .env case-set.json inputs/L3B_CASE_001.json outputs/L3B_CASE_001.json traces/trace.jsonl dist/submission.zip
```

- [ ] Rà lại diff source và tài liệu.
- [ ] Xác nhận `.env`, inputs, outputs, trace và ZIP đều bị ignore.
- [ ] Không commit hoặc push trước khi hoàn tất kiểm tra và duyệt thay đổi.

## Nộp Competition Workspace

Upload `dist/submission.zip` tại workspace `/l3b`, chờ auto scoring hoàn tất,
kiểm tra component breakdown và chọn submission dùng làm final.
