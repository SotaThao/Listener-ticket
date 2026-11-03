# Listener-ticket — Bảng kiểm soát đầu ra dành cho PM

**Version:** v1.0.0 · **Cập nhật:** 03/11/2026 09:12 (Asia/Saigon)

**Owner:** AlexChu · **Người lập:** Codex

**Trạng thái:** Kế hoạch thực hiện; các việc bên dưới chưa nghiệm thu.

**Baseline:** commit cleanup `520a7de`; Owner yêu cầu đưa mã hiện có lên main. Việc publish source không phải phê duyệt vận hành.

## 1. Mốc hiện tại và phạm vi

- Đã có code và kiểm thử offline cho intake, rules cơ bản, pending, reservation, STATUS và một phần lifecycle/ANALYZE/chuyển tiếp SQLite.
- Đã xác nhận: schema chưa cho phép `reused_success`; tái diễn trực tiếp còn HOLD; ANALYZE còn phụ thuộc trích nguyên văn text; chuyển tiếp lịch sử SQLite chỉ hỗ trợ phạm vi hẹp.
- Hai rules mở rộng vẫn DRAFT; bot/config mặc định giữ tắt; trạng thái vận hành **NO_GO**.
- PM dùng bảng này để kiểm output và phụ thuộc. Số lượng test xanh hoặc source đã lên main không tự đóng các việc còn thiếu.
- Các mã PM-xx là mã kiểm soát nội bộ, không phải số issue GitHub mới. Hiện chưa tạo ticket cho phần hoàn thiện offline. Tracking gốc: [Backlog #2178](https://github.com/vlink-group/vlink-nexora/issues/2178); gói setup hiện hữu: [Task #2488](https://github.com/vlink-group/vlink-nexora/issues/2488).

## 2. Phân vai và định nghĩa trạng thái

| Vai | Trách nhiệm |
|---|---|
| Dev / executor | Triển khai, tự kiểm, nộp đầu ra và khai rõ giới hạn; Codex gpt-6.1-sol / Medium theo lựa chọn Owner |
| Verifier | Chạy kiểm độc lập trên đúng source đã đóng băng, đối chiếu output và phạm vi |
| Reviewer | Phiên độc lập, High; đối chiếu nghiệp vụ, quyền và tính toàn vẹn dữ liệu; không sửa source để làm xanh |
| QA / Operator | Kiểm model, bản cài và luồng thật trên môi trường được cho phép; người vận hành máy đích chưa được chỉ định |
| PM | Ghi người nhận việc, lịch dự kiến/thực tế, kiểm đủ output, theo dõi blocker; không tự ký thay Owner về rules/GO |
| Owner | Duyệt rules, cấp quyền môi trường thật và quyết định GO |

Trạng thái dùng: **Todo → Doing → Review → Accepted**. **Blocked** phải ghi nguyên nhân và điều kiện gỡ. `Accepted` chỉ khi đủ tiêu chí và bên nhận nghiệm thu ghi ngày/bằng chứng. Bảng hiện tại không có việc nào được đánh Accepted.

## 3. Bảng công việc và estimate

| ID | Công việc | Người thực hiện / bên nhận | Phụ thuộc | Dev tham chiếu | Với AI | Trạng thái |
|---|---|---|---|---:|---:|---|
| PM-01 | Đồng bộ schema trích dữ kiện | Dev / Reviewer | Baseline | 2–3h | 0,5–0,75h | Todo |
| PM-02 | Hoàn thiện lifecycle ticket đóng | Dev / Reviewer | PM-01 | 4–7h | 0,5–1,25h | Todo |
| PM-03 | Hoàn thiện ANALYZE và hồi quy STATUS | Dev / Reviewer | PM-01 | 6–10h | 1,5–3h | Todo |
| PM-04 | SQLite, ma trận tương thích và cutover legacy | Dev / Reviewer | PM-02, PM-03 | 6–10h | 1,5–3h | Todo |
| PM-05 | Bổ sung CI | Dev / Verifier | Baseline; chốt output cuối sau PM-04 | 2–4h | 0,5–1h | Todo |
| PM-06 | Đóng băng, wheel, regression, review và hồ sơ duyệt | Dev + Verifier + Reviewer / PM | PM-01…PM-05 | 4–6h | 1,5–3h | Todo |
| PM-07 | Duyệt rules mở rộng | Owner / PM ghi quyết định | PM-06 | Không ước tính giờ duyệt | Không ước tính | Blocked: chờ hồ sơ PM-06 |
| PM-08 | Release máy đích, config và rollback | Dev + Operator / QA | PM-06, PM-07; có máy và quyền | 4h | 0,4–0,8h | Blocked: chờ điều kiện |
| PM-09 | Model thật và bộ ví dụ | QA + Dev / Owner | PM-08; model/config được chọn khi setup | 4h | 0,4–0,8h | Blocked: chờ điều kiện |
| PM-10 | Bản cài, service, lock và phục hồi | Operator + QA / PM | PM-08; được phép chạy service test | 4h | 0,4–0,8h | Blocked: chờ điều kiện |
| PM-11 | Telegram → GitHub và hai group | QA + Operator / Owner | PM-09, PM-10; được phép gửi dữ liệu kiểm | 4h | 0,4–0,8h | Blocked: chờ điều kiện |
| PM-12 | Triage, hồi quy và bàn giao preflight | Dev + QA / Owner | PM-08…PM-11 | 4h | 0,4–0,8h | Blocked: chờ kết quả |
| PM-13 | Quyết định vận hành GO/NO_GO | Owner / PM ghi quyết định | PM-12, đủ P1–P13 | Không ước tính giờ duyệt | Không ước tính | Blocked: chưa đủ bằng chứng |
| **Tổng PM-01…PM-06** | **Hoàn thiện offline** | | | **24–40h** | **6–12h** | |
| **Tổng PM-08…PM-12** | **Setup hiện hữu** | | | **20h** | **2–4h** | |
| **Tổng giờ thực hiện** | | | | **44–60h** | **8–16h** | |

Estimate AI là dự báo chưa đo trên toàn bộ phạm vi; không phải cam kết chất lượng hoặc thời gian. Hai cột là hai cách ước tính cho cùng công việc, không cộng với nhau. Các lát AI nhỏ dùng phân bổ sơ bộ để PM theo dõi, sẽ cập nhật sau vòng thực hiện đầu tiên. Không đổi estimate 20h trên ticket setup hiện hữu trong lần publish này.

Lịch dự kiến với AI: **2–3 ngày khi đủ đầu vào**, chưa gắn ngày bắt đầu/kết thúc cố định. Ngày 1 ưu tiên PM-01…PM-03; ngày 2 PM-04…PM-06; ngày 3 dành cho các việc setup đã đủ điều kiện. Đây là thứ tự dự kiến; thiếu quyền/môi trường hoặc review trả finding thì điều chỉnh lịch, không rút bước kiểm.

## 4. Đầu ra và tiêu chí nghiệm thu từng việc

### PM-01 — Schema

**Đầu ra:** bản contract thống nhất và regression đi qua schema thực sự gửi provider.

**Nghiệm thu:** xác định rõ `reused_success` là dữ kiện model hay kết luận host; producer/parser/consumer khớp nhau; test không trả thẳng enum bị schema cấm; không làm đổi contract legacy ngoài phạm vi.

**Bằng chứng:** command, exit code, trường schema quan sát được, kết quả regression, source SHA và reviewer verdict.

### PM-02 — Lifecycle

**Đầu ra:** bảng tình huống open/closed, bằng chứng cũ, tái diễn mới, success đã dùng, episode kế tiếp và thiếu thông tin; implementation tương ứng.

**Nghiệm thu:** bằng chứng và chronology hợp lệ được xử lý theo Business DOCS; không tự reopen; không dùng lại success cũ để tạo trùng; duplicate/restart/POST không rõ kết quả không phát lại thao tác ghi.

**Bằng chứng:** kết quả từng tình huống, định danh nguồn/parent/episode đã che dữ liệu riêng, số lần gọi ghi và review độc lập.

### PM-03 — ANALYZE / STATUS

**Đầu ra:** bảng kết quả cho text, Reply context, ảnh có pixel thật, tài liệu và STATUS theo đúng partition.

**Nghiệm thu:** task đầy đủ, đúng giới hạn, không cần nguồn chứa nguyên văn đáp án; không bịa nguyên nhân hoặc cấp quyền ghi issue; STATUS phân biệt đã xác nhận và chưa rõ kết quả, đúng repo/Reply; không gửi notice trùng.

**Bằng chứng:** regression và smoke contract; chất lượng ảnh/ngữ nghĩa từ model thật để PM-09 kiểm riêng, không ghi PASS từ fake provider thay cho model thật.

### PM-04 — SQLite và legacy

**Đầu ra:** ma trận từng loại dữ liệu cũ với quyết định: chuyển tự động có chứng cứ / đối soát thủ công / chặn cutover; thử nghiệm trên bản sao.

**Nghiệm thu:** kiểm digest/receipt, schema, hydration, pending, notice/lựa chọn cũ, attachment và reservation; từ chối phải rollback; giữ dữ liệu và không replay ghi chưa rõ kết quả. Mọi khoảng trống phải được ghi rõ; không coi chặn an toàn là đã chuyển thành công.

**Điều kiện gỡ legacy:** ma trận hành vi không còn khoảng trống nghiệp vụ, có đường giữ/đọc/đối soát dữ liệu cũ và rollback đã thử. Nếu chưa đạt, giữ legacy và đánh việc cutover chưa hoàn tất.

**Bằng chứng:** checksum trước/sau, output từng case, trạng thái sau restart và reviewer verdict.

### PM-05 — CI

**Đầu ra:** workflow kiểm Python 3.11/3.12 trên Windows/Linux, policy và regression cần thiết.

**Nghiệm thu:** pipeline thực sự chạy được trên đúng source; test skip/fail có tên và lý do; lỗi kiểm tra làm job thất bại đúng.

**Bằng chứng:** liên kết CI run, commit SHA, từng job và kết quả. Nếu chỉ kiểm được cấu hình local thì ghi Review/Blocked, chưa Accepted.

### PM-06 — Bản đóng gói và review

**Đầu ra:** source đã đóng băng, wheel kiểm thử, bảng fingerprint/resource, báo cáo checks, finding và previews rules/ví dụ để duyệt.

**Nghiệm thu:** cài wheel vào môi trường tạm, import/resource thực sự từ bản cài; CLI và checks khớp; verifier và reviewer độc lập; mọi finding mức cao được xử lý hoặc chuyển Owner quyết định rõ. Không thay hash/trace để tạo kết luận tương đương nghiệp vụ.

**Bằng chứng:** SHA và checksum wheel, lệnh cài/kiểm, log tóm tắt, scope diff, danh sách skip, verdict theo đúng phiên bản. Rules mới giữ DRAFT cho tới PM-07.

### PM-07 — Phê duyệt rules

**Đầu ra:** quyết định Owner nêu đúng phiên bản rules, ví dụ và câu chữ đã xem.

**Nghiệm thu:** ghi ngày/người duyệt; chỉ áp dụng ACTIVE cho đúng bản được duyệt. Phê duyệt rules chưa cấp GO vận hành.

### PM-08…PM-12 — Gói setup 20h

| ID | Output PM cần nhận | Tiêu chí nhận |
|---|---|---|
| PM-08 | Phiếu release/config đã che bí mật và biên bản rollback | Xác định đúng SHA/wheel/config; rollback đã chạy và giữ nguyên dữ liệu cần giữ |
| PM-09 | Bảng từng ví dụ model thật: expected, actual, PASS/FAIL, lỗi | Dùng đúng cấu hình được duyệt; không suy chất lượng từ schema hợp lệ; fail giữ NO_GO |
| PM-10 | Readback service/PID/fingerprint và test restart/lock | Đúng bản cài; một tiến trình/DB; phục hồi không gây ghi trùng |
| PM-11 | Biên bản từng luồng Telegram/GitHub và hai group | Dùng tin kiểm mới đã cho phép; ticket/ảnh/Reply đúng nguồn; text bot đúng ops group; không replay lịch sử thật |
| PM-12 | Danh sách finding, kết quả regression và phiếu preflight P1–P13 | Mọi mục có bằng chứng hoặc trạng thái thiếu/chặn rõ; chỉ đề xuất GO khi đủ điều kiện |

Ánh xạ tracking hiện hữu: PM-08 → [#2489](https://github.com/vlink-group/vlink-nexora/issues/2489); PM-09 → [#2490](https://github.com/vlink-group/vlink-nexora/issues/2490); PM-10 → [#2491](https://github.com/vlink-group/vlink-nexora/issues/2491); PM-11 → [#2492](https://github.com/vlink-group/vlink-nexora/issues/2492); PM-12 → [#2493](https://github.com/vlink-group/vlink-nexora/issues/2493). Đây là ánh xạ theo hồ sơ đã tạo; lần publish này không thay trạng thái hoặc Effort trên GitHub.

### PM-13 — GO

**Đầu ra:** quyết định GO/NO_GO có người ký và phiên bản áp dụng.

**Nghiệm thu:** Owner ký khi đủ phiếu preflight; commit/push main hoặc test offline xanh không thay quyết định này.

## 5. Mẫu cập nhật để PM kiểm soát

Mỗi lần bàn giao phải có:

```text
Mã việc: PM-xx
Người nhận việc / người nghiệm thu:
Trạng thái: Todo | Doing | Review | Accepted | Blocked
Source SHA / branch:
Estimate dev / AI; thời gian thực tế đã dùng:
Output đã nộp: đường dẫn hoặc artifact URL
Tiêu chí đã đạt / chưa đạt:
Lệnh kiểm + exit code + số PASS/FAIL/SKIP:
Reviewer/verifier và verdict:
Blocker; người gỡ; điều kiện gỡ:
Việc tiếp theo và thời điểm cập nhật tiếp:
```

PM chỉ ghi Accepted sau khi đối chiếu output với tiêu chí. Không đóng việc theo câu “đã xong” của executor. Không tính giờ tiết kiệm AI thành giờ thực tế. Artifact log/SQLite/session/token không đưa vào commit; bản tóm tắt kiểm chứng và link tới bằng chứng có thể đưa vào tài liệu review.

## 6. Phạm vi mở rộng và giới hạn

Installer .exe/wizard, adapter Jira/Azure và refactor module ngoài nhu cầu tương đương nghiệp vụ là backlog mở rộng, cần scope và estimate riêng. Không tự gộp vào 44–60h / 8–16h. Công cụ harness-verify hiện yêu cầu Node; repo Python phải ghi giới hạn này, dùng checks Python thật và không giả PASS của gate.

## 7. Related Documents

- [Master overview](../00-master/telegram-triage-noissue_overview_261027_v1.0.0.md)
- [Business DOCS](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md)
- [Engineering / nghiên cứu](../02-engineering/telegram-triage-noissue_thinking-research_261015_v1.0.0.md)
- [QA acceptance](../03-qa/telegram-triage-noissue_qa_261015_v1.0.0.md)
- [Audit và gói setup](../02-engineering/listener-installer-noissue_devex-audit_261030_v1.0.0.md)
- [Rules spec hiện hữu](telegram-triage-noissue_rules-spec_261029_v1.0.0.md)
- [Báo cáo tổng thể](listener-ticket-2178_report_261103_v1.0.2.md)

## 8. Changelog

| Version | Ngày giờ (Asia/Saigon) | Tác giả | Nội dung |
|---|---|---|---|
| v1.0.0 | 03/11/2026 09:12 | Codex / Owner AlexChu | Chia 13 việc với output, acceptance, dependencies, trách nhiệm và estimate; giữ NO_GO |
