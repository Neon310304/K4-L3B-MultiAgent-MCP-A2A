# Báo cáo triển khai K4 L3B — Multi-Agent MCP + A2A

## Thông tin

| Trường | Nội dung |
| --- | --- |
| Học viên | Trần Quốc Vượng |
| Mã học viên | `2A202602522` |
| Lớp | `H209` |
| Variant | `l3b` |
| Output contract | `day09-l3b-output-v2` |
| Phiên bản | `1.0.0` |

## Phạm vi hoàn thành

| Pha | Hạng mục | Thành phần nghiệm thu |
| --- | --- | --- |
| 1 | Môi trường Python và CLI | `.venv`, package editable, `day09 --help` |
| 2 | Multi-agent A2A và state contract | `ARCHITECTURE.md`, `state.py` |
| 3 | Specialist agents và MCP Gateway | `agents.py`, `mcp_gateway.py`, evidence trace |
| 4 | Policy, verifier và calibration | `policy.py`, kiểm tra consistency/confidence |
| 5 | Batch 100 case và validation | staging batch, output/trace integrity checks |
| 6 | Submission packaging | manifest, trace, outputs-only ZIP |

## Phân công

| Vai trò | Người phụ trách | Trách nhiệm |
| --- | --- | --- |
| Coordinator/Router | Trần Quốc Vượng (`2A202602522`) | Điều phối lifecycle, task và handoff |
| Entity/Customer Agent | Trần Quốc Vượng (`2A202602522`) | Resolve order, customer context |
| Order/Item/Seller Agent | Trần Quốc Vượng (`2A202602522`) | Thu thập order, item, seller evidence |
| Shipment Agent | Trần Quốc Vượng (`2A202602522`) | Phân tích timeline và trách nhiệm giao hàng |
| Payment/Refund Agent | Trần Quốc Vượng (`2A202602522`) | Đối soát capture, split payment và refund |
| Policy Agent | Trần Quốc Vượng (`2A202602522`) | Xác định issue, trách nhiệm, refund và actions |
| Verifier Agent | Trần Quốc Vượng (`2A202602522`) | Kiểm tra schema, consistency và confidence |
| MCP/Trace/Packaging | Trần Quốc Vượng (`2A202602522`) | Provenance, audit trace và submission ZIP |

## Thiết kế và quyết định kỹ thuật

Workflow sử dụng Python async state machine. Coordinator gọi các specialist
theo lifecycle cố định; `CaseState` khóa case scope, query budget và phase.
`EvidenceCollector` là điểm duy nhất được gọi MCP, áp dụng permission theo
agent, cache theo case và phát `tool_result_consumed` cho response hợp lệ.

Gateway discovery được cache và mọi tool argument được kiểm tra theo input
schema do server công bố. Evidence envelope được kiểm tra bằng JSON Schema;
`evidence_ref` được giữ nguyên và ràng buộc cục bộ với `case_id` cùng
`result_hash` để phát hiện reuse sai scope.

Policy Engine đưa ra quyết định deterministic từ evidence đã chuẩn hóa.
Verifier kiểm tra primary issue, responsible parties, refund lines, actions,
evidence linkage và case status. Confidence được hiệu chuẩn theo coverage,
entity resolution, timeline và data conflicts; hệ thống không phát confidence
`1.0`.

Batch chạy qua staging và chỉ công bố kết quả sau khi toàn bộ case thành công.
Validator kiểm tra inventory output, schema, case scope, event uniqueness,
lifecycle ordering, evidence-to-trace linkage và secret leakage. Packager chỉ
đưa `manifest.json`, `trace.jsonl` và `outputs/*.json` vào ZIP.

## Quy trình nghiệm thu

```powershell
.venv\Scripts\Activate.ps1
pytest -q
ruff check src tests
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Batch thật sử dụng bundle L3B và thông tin MCP được cấp qua Competition
Workspace. Các dữ liệu runtime và secret nằm ngoài Git theo `.gitignore`.

## Tiêu chí hoàn tất

- Đủ đúng 100 input và 100 output cùng case set.
- Mọi output pass `l3b-output-v2.schema.json`.
- Mọi evidence ref có `tool_result_consumed` cùng case.
- Lifecycle đi từ `case_received` đến `case_finalized` đúng thứ tự.
- Refund lines khớp tổng hoàn tiền và responsibility khớp primary issue.
- Submission ZIP không chứa source, input, `.env`, key hoặc debug log.

## Đối chiếu tiêu chí chấm điểm

| Thành phần | Trọng số | Cơ chế đáp ứng |
| --- | ---: | --- |
| Semantic | 40% | Issue precedence, shipment timeline, payment reconciliation và refund deterministic |
| Evidence | 15% | Evidence theo domain; kết luận được hạ về `insufficient_evidence` nếu thiếu domain bắt buộc |
| Provenance | 15% | Gateway giữ nguyên ref, bind case/hash và trace `tool_result_consumed` |
| Consistency | 10% | Verifier kiểm tra issue, party, status, refund lines và actions |
| Schema | 5% | Validate output, trace, evidence envelope và manifest bằng public contracts |
| Calibration | 5% | Confidence theo coverage, entity, timeline và conflict caps |
| Workflow | 5% | Validator yêu cầu đủ bảy lifecycle events theo đúng thứ tự |
| Efficiency | 5% | Discovery cache, cache theo case, bounded retry và query budget 12 |

Các hard gate được chặn trước khi đóng gói: `case_id` phải khớp, output phải
pass schema, case phải có authoritative MCP evidence, mọi ref phải được gateway
phát hành và xuất hiện trong trace cùng case. Cross-case hoặc hash mismatch bị
Gateway từ chối trước khi evidence vào state.
