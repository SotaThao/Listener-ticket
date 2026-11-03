# Listener-ticket — Ba task chức năng trước đóng gói

**Version:** v1.0.11 · **Cập nhật:** 03/11/2026 16:51 (Asia/Saigon)

**Owner:** AlexChu · **Trạng thái:** Ba task qua review source và full regression; chờ PM nghiệm thu.

## 1. Mục tiêu đợt này

Hoàn thiện schema/lifecycle, ANALYZE/STATUS và SQLite/legacy để chạy kiểm chức năng từ source trước khi đóng gói. Baseline trên main: `e2833ec`. CI, đóng gói/review bản cài và setup vận hành không thuộc plan đợt này; các ticket setup đã có giữ nguyên.

Đã có code một phần và test offline xanh; lượt sửa các khoảng trống bắt đầu ngày 03/11/2026 trong worktree `Listener-ticket-functional-2178`, branch `bugfix/2178_rules-functional-completion`. Publish main không đồng nghĩa ba task đã hoàn thành. Rules mở rộng vẫn DRAFT; khi chạy thật cần đúng cấu hình, môi trường kiểm và rules được duyệt.

### Hiểu nhanh ba task

1. **Schema và lifecycle:** thống nhất khuôn dữ liệu model trả cho bot và quy tắc bổ sung/hỏi/tạo lần tái diễn theo vòng đời ticket.
2. **ANALYZE / STATUS:** phân tích nguồn thành đề xuất việc cần làm và tra trạng thái ghi nhận/xử lý trong sổ bot, đúng yêu cầu và ticket.
3. **SQLite và legacy:** chuyển tiếp an toàn dữ liệu cục bộ và trạng thái từ luồng cũ sang rules mới, tránh mất dữ liệu hoặc ghi trùng.

[Định nghĩa từng thuật ngữ và phạm vi thực hiện](listener-ticket-2178_report_261103_v1.0.2.md#định-nghĩa-ba-nhóm-việc).

[Nơi lưu rules/code/dữ liệu, cách hoạt động và chỉnh thủ công](listener-ticket-2178_report_261103_v1.0.2.md#8-tra-cứu-lưu-trữ-hoạt-động-và-chỉnh-thủ-công).

## 2. Bảng PM kiểm soát

| ID | Task | Output phải nộp | Tiêu chí nhận | Phụ thuộc | Agent dự kiến | Trạng thái |
|---|---|---|---|---|---:|---|
| PM-01 | Schema và lifecycle | Contract trích dữ kiện thống nhất; bảng kết quả ticket đóng, bằng chứng cũ/tái diễn, success đã dùng và episode kế tiếp | Schema/provider/parser khớp; hành động đúng Business DOCS; duplicate/restart/ghi chưa rõ kết quả không gây ticket trùng | Baseline | 1–2h | Review — High source APPROVED |
| PM-02 | ANALYZE / STATUS | Kết quả text, ảnh có pixel thật, tài liệu và Reply context; trạng thái ticket đúng nguồn | Task không đòi nguồn chứa nguyên văn đáp án; đầu ra đủ và có giới hạn; STATUS đúng repo/partition; không tự cấp quyền ghi | PM-01 | 1,5–3h | Review — High source APPROVED |
| PM-03 | SQLite và legacy | Ma trận chuyển tiếp lịch sử; thử trên bản sao; kết quả chạy các luồng từ source và đề xuất giữ/gỡ legacy | Có kiểm digest/receipt, pending/notice/attachment/reservation; từ chối có rollback; không mất dữ liệu hoặc replay ghi chưa rõ kết quả; mọi giới hạn được ghi rõ | PM-01, PM-02 | 1,5–3h | Review — High source APPROVED R3 |
| **Tổng** | **Ba task chức năng** | | | | **4–8h** | |

Estimate gồm triển khai, chạy kiểm và review từng task; là dự báo ban đầu, kết quả đo thực tế ghi riêng bên dưới. Không cộng với giờ công tham chiếu hoặc tự đổi Effort trên board. Các mã PM là mã nội bộ; phiên bản này gộp schema/lifecycle thành PM-01, ANALYZE/STATUS thành PM-02, SQLite/legacy thành PM-03; bảng 13 mã cũ chỉ còn giá trị lịch sử và không phải bộ việc đang giao.

**Cập nhật thực tế 16:51:** Cả ba task High **APPROVED trong phạm vi source đã kiểm**. Full R3 chạy 1.650 test: **1.648 pass, 2 skip**, exit 0 / 537,600 giây; 130 file không đổi khi chạy. PM-01 ~114 phút, PM-02 ~74 phút, PM-03 ~121 phút tới kiểm tổng cuối; lượt triển khai 11:36–16:50 ~5h14 elapsed, không cộng giờ Agent song song. Legacy vẫn giữ cho lịch sử chưa hỗ trợ. Chưa có PM ký nhận nên 0/3 Accepted; real-model/live quality chưa xác minh.

## 3. Cách thực hiện và bằng chứng

[Plan triển khai chi tiết ba task](listener-completion-2178/plan.md): bước sửa, mapping code, checklist, ca kiểm và mốc nhận từng output. Owner đã yêu cầu “bắt đầu tiến hành”; ba task qua review source và kiểm tổng, chưa có PM ký nghiệm thu.

- **Executor:** Codex gpt-6.1-sol / Medium, triển khai một stream tuần tự vì các task dùng chung contract và dữ liệu.
- **Verifier/reviewer:** phiên độc lập / High; chạy kiểm và đối chiếu nghiệp vụ. Finding được trả về executor sửa.
- **PM:** nhận từng task khi đủ source SHA, output, command/exit code, PASS/FAIL/SKIP và reviewer verdict; ghi người nhận/ngày nhận và blocker.
- **Owner:** duyệt thay đổi rules và việc chạy trên môi trường thật khi cần. Không tự đổi DRAFT để vượt lỗi startup.

Task PM-01 phải kiểm schema thật gửi provider, tránh model giả trả thẳng enum bị cấm. PM-02 phải tách kiểm contract giả lập với chất lượng model thật. PM-03 phải phân biệt chuyển được, cần đối soát và phải chặn; chặn an toàn không đồng nghĩa đã chuyển thành công. Chỉ gỡ legacy khi đã chứng minh đủ nghiệp vụ và đường xử lý dữ liệu cũ.

Đã nộp kết quả chạy chức năng từ source cho cả ba luồng và full regression cuối. Nếu thiếu model/môi trường thì ghi rõ ca chưa chạy; không coi test offline là E2E Telegram/model/GitHub thật. Chưa yêu cầu build wheel, installer, CI hoặc setup service trong đợt này.

## 4. Mẫu cập nhật tiến độ

```text
Mã task / người thực hiện / người nghiệm thu:
Trạng thái: Todo | Doing | Review | Accepted | Blocked
Source SHA / branch:
Output đã nộp:
Lệnh chạy + exit code + PASS/FAIL/SKIP:
Reviewer verdict:
Thời gian Agent thực tế / estimate còn lại:
Phần chưa xác minh / blocker / việc tiếp theo:
```

## 5. Related Documents

- [Master overview](../00-master/telegram-triage-noissue_overview_261027_v1.0.0.md)
- [Business DOCS](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md)
- [Engineering](../02-engineering/telegram-triage-noissue_thinking-research_261015_v1.0.0.md)
- [QA](../03-qa/telegram-triage-noissue_qa_261015_v1.0.0.md)
- [Báo cáo](listener-ticket-2178_report_261103_v1.0.2.md)
- [Tracking gốc #2178](https://github.com/vlink-group/vlink-nexora/issues/2178)

## 6. Changelog

| Version | Ngày giờ (Asia/Saigon) | Tác giả | Nội dung |
|---|---|---|---|
| v1.0.11 | 03/11/2026 16:51 | Codex / Owner AlexChu | Ba task source APPROVED; full R3 1648 pass/2 skip, elapsed ~5h14; giữ legacy và chờ PM nhận |
| v1.0.10 | 03/11/2026 16:03 | Codex / Owner AlexChu | PM-03 R1 CHANGES_NEEDED, hai full suite cùng lỗi restart; R2 đang sửa, chưa nghiệm thu |
| v1.0.9 | 03/11/2026 14:47 | Codex / Owner AlexChu | PM-02 High source APPROVED sau hoàn tất kiểm file-backed; bắt đầu PM-03, giữ giới hạn live/global gate |
| v1.0.8 | 03/11/2026 13:31 | Codex / Owner AlexChu | PM-01 High R5 source APPROVED; bắt đầu PM-02, phân biệt source review với PM Accepted và global gate |
| v1.0.7 | 03/11/2026 13:08 | Codex / Owner AlexChu | Cập nhật PM-01 R4 và hai lỗi High R3; giữ 0/3 nghiệm thu, chưa triển khai PM-02/03 |
| v1.0.6 | 03/11/2026 12:23 | Codex / Owner AlexChu | Ghi tiến độ PM-01 R2 và finding độc lập; tiếp tục R3, chưa nghiệm thu |
| v1.0.5 | 03/11/2026 11:37 | Codex / Owner AlexChu | Bắt đầu PM-01 trong worktree riêng; giữ hai task sau Todo và 0/3 nghiệm thu |
| v1.0.4 | 03/11/2026 11:17 | Codex / Owner AlexChu | Bổ sung plan triển khai tuần tự ba task, đầu ra, kiểm chứng và mốc nhận |
| v1.0.3 | 03/11/2026 09:35 | Codex / Owner AlexChu | Liên kết hướng dẫn vị trí lưu trữ, hoạt động và chỉnh thủ công cho ba task |
| v1.0.2 | 03/11/2026 09:32 | Codex / Owner AlexChu | Thêm diễn giải ngắn ba task và liên kết tới định nghĩa chi tiết |
| v1.0.1 | 03/11/2026 09:27 | Codex / Owner AlexChu | Owner thu hẹp đợt này còn ba task để chạy từ source trước đóng gói; thay bảng 13 mã bằng ba đầu ra nghiệm thu, estimate 4–8h |
| v1.0.0 | 03/11/2026 09:12 | Codex / Owner AlexChu | Kế hoạch tổng thể cũ gồm 13 mã việc |
