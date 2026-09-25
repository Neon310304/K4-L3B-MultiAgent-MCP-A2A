# K4 L3B — Multi-Agent MCP + A2A

## Thông tin thực hiện

| Trường | Nội dung |
| --- | --- |
| Học viên | Trần Quốc Vượng |
| Mã học viên | `2A202602522` |
| Lớp | `H209` |
| Biến thể bài | K4 L3B |
| Phiên bản triển khai | `1.0.0` |

Toàn bộ workflow, contract integration, evidence collection, kiểm chứng và quy
trình đóng gói của bài được triển khai trong repository này.

## Mục tiêu

Xây dựng hệ thống multi-agent điều tra khiếu nại thương mại điện tử.

Ngoài kết luận nghiệp vụ, yêu cầu cần phải xử lý xử lý entity resolution, customer context, shipment/payment analysis, source conflict và hiệu quả sử dụng MCP.

## Dữ liệu

Tham khảo dữ liệu tại: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce

## Repository

Repository giữ nguyên tên chính thức `K4-L3B-MultiAgent-MCP-A2A`.

## Phân công thực hiện

| Hạng mục | Người phụ trách | Kết quả |
| --- | --- | --- |
| Coordinator, routing và A2A state | Trần Quốc Vượng (`2A202602522`) | `workflow.py`, `state.py` |
| Entity resolution và customer context | Trần Quốc Vượng (`2A202602522`) | `EntityAgent`, `CustomerAgent` |
| Order, item và seller investigation | Trần Quốc Vượng (`2A202602522`) | `OrderItemAgent`, `SellerAgent` |
| Shipment investigation | Trần Quốc Vượng (`2A202602522`) | `ShipmentAgent` |
| Payment và refund reconciliation | Trần Quốc Vượng (`2A202602522`) | `PaymentAgent` |
| MCP Evidence Gateway và provenance | Trần Quốc Vượng (`2A202602522`) | `mcp_gateway.py`, `EvidenceCollector` |
| Policy Engine, Verifier và calibration | Trần Quốc Vượng (`2A202602522`) | `policy.py` |
| Trace, schema validation và packaging | Trần Quốc Vượng (`2A202602522`) | `trace.py`, `submission.py`, `cli.py` |
| Kiểm thử và tài liệu | Trần Quốc Vượng (`2A202602522`) | `tests/`, `README.md`, `ARCHITECTURE.md` |

## 1. Cài đặt

Yêu cầu Python 3.11 trở lên.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Trên Windows PowerShell dùng:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Kiểm tra:

```bash
pytest -q
day09 --help
```

## 2. Cấu hình Competition và MCP

Mở `/register` trên Competition Workspace, điền mã học viên `2A202602522`,
nhập registration code được cấp cho lớp L3B và lưu Team API Key dạng
`sk-team-...` được hiển thị sau khi đăng ký.

Điền thông tin thật vào `.env`:

```dotenv
COMPETITION_API_URL=http://127.0.0.1:8081
COMPETITION_TEAM_API_KEY=sk-team-your_key
MCP_ENDPOINT=http://127.0.0.1:8001/mcp
```

Giữ `.env` ở máy chạy bài và không commit file này. `COMPETITION_TEAM_API_KEY`
không được đặt trong README, source, output hoặc trace.

## 3. Tải input

Tải ZIP input **L3B** từ GitHub Release và giải nén vào root repo:

```bash
unzip l3b-inputs-<version>.zip -d .
day09 validate-inputs
```

PowerShell:

```powershell
Expand-Archive .\l3b-inputs-<version>.zip -DestinationPath . -Force
day09 validate-inputs
```

Cấu trúc đúng:

```text
case-set.json
inputs/
├── L3B_CASE_001.json
├── ...
└── L3B_CASE_100.json
```

Một số case không cung cấp exact order ID. Agent phải dùng candidate và evidence để resolve entity.

## 4. Sử dụng MCP

MCP Gateway cung cấp evidence về order, customer, product, shipment, payment, refund và policy. Mọi call đều được server audit theo team và case.

Xem các tool hiện có:

```bash
day09 mcp-tools
```

Ví dụ gọi tool trong `workflow.py`:

```python
evidence = await gateway.call(
    "get_customer_history",
    case_id=case["case_id"],
    customer_unique_id=customer_unique_id,
)

evidence_ref = evidence["evidence_ref"]
customer_data = evidence["data"]
```

Khi dùng evidence, ghi lại trong trace:

```python
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="entity-agent",
    tool_name="get_customer_history",
    evidence_refs=[evidence_ref],
)
```

Quy tắc quan trọng:

- luôn truyền đúng `case_id`;
- dùng tool discovery, không đoán tên tool;
- không sửa hoặc tự tạo `evidence_ref`;
- không dùng evidence chéo case;
- giới hạn retry, cache trong phạm vi case và tránh gọi tool thừa.

Tất cả MCP calls đều được audit và có thể ảnh hưởng điểm efficiency, kể cả call không được đưa vào output.

## 5. Xây dựng multi-agent workflow

Triển khai tại:

```text
src/student_agent/workflow.py
```

Hàm chính:

```python
async def solve_case(case, gateway, trace) -> dict:
    ...
```

Workflow triển khai các vai trò:

- Coordinator/Router;
- Entity và Customer Agent;
- Order/Item và Seller Agent;
- Shipment Agent;
- Payment/Refund Agent;
- Policy Agent;
- Verifier Agent.

Competition không chấm tên framework hay số lượng class. Scorer đánh giá output, evidence, efficiency và sự phối hợp thể hiện trong trace.

Trace chỉ ghi sự kiện quan sát được như `task_assigned`, `handoff`, `tool_result_consumed`, `verification_completed`.

Hoàn thiện mô tả thiết kế trong `ARCHITECTURE.md`.

Workflow hiện thực sử dụng state machine async thuần Python để giữ luồng chạy
deterministic. `CaseState` khóa `case_id`, phase, evidence refs, handoff và
query budget; `EvidenceCollector` là điểm duy nhất gọi MCP và ghi
`tool_result_consumed`. Các agent chuyên trách chỉ được gọi tool trong scope
đã khai báo.

`PolicyEngine` nạp và kiểm tra `contracts/scoring/scoring-policy-v2.json`, sau
đó xác định primary issue, responsible parties, số tiền hoàn và resolution
actions bằng quy tắc deterministic. `Verifier` kiểm tra nhất quán giữa các
field và hiệu chuẩn confidence theo độ phủ evidence, trạng thái entity,
timeline và data conflicts. Confidence tối đa là `0.95`, bị giới hạn `0.75`
khi có mâu thuẫn và `0.69` khi thiếu evidence bắt buộc.

## 6. Chạy và kiểm tra

```bash
day09 run
day09 validate
```

`day09 run` xóa output cũ, xử lý lần lượt từng case trong `case-set.json`, ghi
output theo case và ghi toàn bộ event vào `traces/trace.jsonl`. Trước khi chạy
batch cần bảo đảm `day09 mcp-tools` liệt kê được catalog tool và
`day09 validate-inputs` báo đúng `l3b / <version> / 100 cases`.

Batch được tạo trong thư mục staging. Chỉ khi cả 100 case hoàn thành thì
`outputs/` và `traces/trace.jsonl` mới được thay bằng kết quả mới; nếu một case
thất bại, kết quả hợp lệ của lần chạy trước vẫn được giữ. `day09 validate` còn
kiểm tra mỗi output evidence ref phải xuất hiện trong event
`tool_result_consumed` cùng case và lifecycle phải đủ, đúng thứ tự.

Kết quả được tạo tại:

```text
outputs/<case_id>.json
traces/trace.jsonl
```

Nếu output pass schema nhưng điểm thấp, cần kiểm tra semantic, entity resolution, evidence, consistency, confidence, workflow và số MCP calls.

## 7. Đóng gói và nộp bài

```bash
day09 package --output dist/submission.zip
```

ZIP chỉ được chứa:

```text
submission.zip
├── manifest.json
├── trace.jsonl
└── outputs/
    ├── L3B_CASE_001.json
    ├── ...
    └── L3B_CASE_100.json
```

Không đưa source, input, `.env`, API key hoặc debug log vào ZIP. Sau đó upload `dist/submission.zip` tại workspace `/l3b` và chọn submission muốn dùng làm final.

Packager ghi vào file tạm, đọc lại toàn bộ archive để kiểm tra inventory và
payload, rồi mới thay thế `dist/submission.zip`. ZIP không có thư mục bọc ngoài.

Trình tự nghiệm thu cuối:

```bash
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

## Tiêu chí chấm điểm công khai

| Thành phần                                     | Trọng số |
| ---------------------------------------------- | -------: |
| Độ đúng nghiệp vụ (`semantic`)                 |      40% |
| Chất lượng bằng chứng (`evidence`)             |      15% |
| Evidence đúng MCP audit (`provenance`)         |      15% |
| Tính nhất quán giữa các field (`consistency`)  |      10% |
| Đúng JSON Schema (`schema`)                    |       5% |
| Confidence hợp lý (`calibration`)              |       5% |
| Quy trình multi-agent trong trace (`workflow`) |       5% |
| Hiệu quả gọi tool (`efficiency`)               |       5% |

Case có thể nhận 0 điểm nếu:

- sai `case_id` hoặc output không thể chấm theo schema;
- thiếu evidence bắt buộc;
- evidence ref không tồn tại;
- evidence thuộc team, run hoặc case khác.

## Tài liệu triển khai

- Thiết kế hệ thống và invariant: `ARCHITECTURE.md`.
- Báo cáo implementation và nghiệm thu: `IMPLEMENTATION_REPORT.md`.
- Checklist đóng gói và nộp bài: `SUBMISSION_CHECKLIST.md`.
- Nguồn contract chuẩn: `contracts/README.md` và `contracts/schemas/`.
