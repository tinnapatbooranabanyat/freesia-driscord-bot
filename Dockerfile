FROM python:3.12-slim

# ffmpeg = แปลงเสียงส่งเข้า Discord, deno = JS runtime ที่ yt-dlp ต้องใช้ถอดรหัสลิงก์ YouTube
# fonts-noto-* = ฟอนต์ไทย/ญี่ปุ่น/เกาหลี/จีน สำหรับรูปการ์ด "กำลังเล่น"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
