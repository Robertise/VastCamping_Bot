# Vast.ai VN GPU Bot

Bot Telegram báo khi có máy GPU trên Vast.ai đạt điều kiện:
**Việt Nam, 1 GPU, on-demand, verified, độ tin cậy ≥ 90%**, đúng loại GPU mỗi người chọn.

- Mỗi người dùng Telegram có danh sách GPU, giá tối đa và trạng thái tạm dừng riêng.
- Mỗi chu kỳ (mặc định 60 giây) chỉ gọi Vast.ai **1 request** cho tất cả người dùng.
- Có máy mới thì gửi tin gồm **toàn bộ** máy đang khớp danh sách của bạn, máy mới đánh dấu 🆕 và xếp lên đầu.
- Khi bạn thêm GPU, `/resume` hoặc đổi `/maxprice`, bot gửi ngay danh sách máy đang có.
- Nếu API lỗi liên tục quá 15 phút, bot nhắn cảnh báo để bạn biết nó đang không theo dõi được.

## Lệnh

| Lệnh | Chức năng |
|---|---|
| `/gpu 5070ti 3090` | Thêm GPU |
| `/list` | Xem GPU đang theo dõi |
| `/remove 3090` | Xóa một vài GPU |
| `/clear` | Xóa hết (có nút xác nhận) |
| `/now` | Máy đang có khớp danh sách của bạn |
| `/vn` | Tất cả GPU đang có ở VN kèm lệnh `/gpu` tương ứng |
| `/maxprice 0.4` / `/maxprice off` | Giới hạn giá $/giờ |
| `/pause` / `/resume` | Tạm dừng / tiếp tục (giữ nguyên danh sách) |
| `/status` | Lần kiểm tra gần nhất, lỗi nếu có |

Tên GPU: `5070ti` = `RTX 5070 Ti` = `rtx5070ti`. `3090` không khớp `3090 Ti`.
Thêm `*` để khớp mọi biến thể, ví dụ `h100*` khớp cả `H100 SXM` và `H100 PCIE`.
Không chắc tên thì gõ `/vn` để xem.

## 1. Chuẩn bị

1. **Telegram bot token:** nhắn `@BotFather` → `/newbot` → lấy token.
2. **Vast.ai API key:** https://cloud.vast.ai/manage-keys/
3. Copy `.env.example` thành `.env` rồi điền 2 giá trị trên.

## 2. Test API trên máy của bạn (nên làm trước)

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python check_api.py US    # US nhiều máy, để chắc chắn API chạy đúng
python check_api.py VN    # xem hiện có máy VN nào
```

Cần xem 3 thứ trong output:
- `HTTP 200` và dòng `✅ Sau khi lọc: N máy`.
- Không có cảnh báo `Server KHÔNG lọc theo quốc gia`.
- Danh sách tên GPU kèm lệnh `/gpu ...` tương ứng.

Muốn thử cả bot trên máy mình: đặt `COUNTRY=US` trong `.env`, chạy `python bot.py`, nhắn `/gpu 4090` cho bot. Test xong thì đổi lại `COUNTRY=VN`.

## 3. Deploy lên AWS EC2

**Cấu hình khuyên dùng:** region **us-east-1 (N. Virginia)**, loại máy **t4g.micro**, **Ubuntu 24.04 (arm64)**, ổ 8 GB gp3.
Chi phí khoảng $10/tháng (máy ~$6.1, IPv4 ~$3.65, ổ đĩa <$1), nên $150 credit dùng được khoảng hơn một năm.
Bot không cần độ trễ thấp, nên chọn region rẻ nhất thay vì Singapore.

1. EC2 → Launch instance: chọn như trên. Tạo key pair để SSH.
   Security group chỉ cần mở **SSH (22)** cho IP của bạn. Bot không cần mở cổng nào vì nó tự gọi ra Telegram.
2. Đưa code lên máy (qua git clone repo của bạn, hoặc `scp`):
   ```bash
   ssh -i key.pem ubuntu@<IP>
   sudo apt update && sudo apt install -y python3-venv git
   git clone <repo-của-bạn> vast-vn-bot && cd vast-vn-bot
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   nano .env        # dán nội dung .env (không commit file này lên git)
   .venv/bin/python check_api.py VN
   ```
3. Chạy như service để tự khởi động lại khi lỗi hoặc reboot:
   ```bash
   sudo cp vast-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now vast-bot
   journalctl -u vast-bot -f      # xem log
   ```
4. Cập nhật code sau này: `git pull && sudo systemctl restart vast-bot`.

**Nên làm thêm:** vào Billing → Budgets, tạo budget $15/tháng có cảnh báo email, để phát hiện sớm nếu có gì tốn tiền bất thường.
Kiểm tra thêm ngày hết hạn của credit. Nếu tài khoản đang ở **Free plan**, AWS có thể đóng tài khoản sau 6 tháng nếu bạn không nâng lên Paid plan.

## Giới hạn đã biết

- Chưa gọi thử API thật: logic đã test bằng dữ liệu giả, nên chạy `check_api.py` trước.
- Bot chỉ chạy **1 process**. Đừng chạy 2 bản cùng token, Telegram sẽ báo lỗi conflict.
- Link trong tin nhắn mở trang thuê chung của Vast.ai. Dùng ID máy / offer trong tin để tìm đúng máy.
- Từ lúc có thông báo đến lúc bạn bấm thuê vẫn có thể bị người khác thuê trước.
- `ALLOWED_CHAT_IDS` để trống thì ai tìm ra bot cũng dùng được. Muốn khóa lại thì điền chat ID của bạn và teammate, chat ID xem được qua `@userinfobot`.
