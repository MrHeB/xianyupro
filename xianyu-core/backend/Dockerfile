FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# 1688 扫码登录要真浏览器（见 app/auth/alibaba_login.py 顶部说明）：
# --with-deps 顺带装 chromium 需要的系统库，分开写会在 slim 里缺 lib 起不来。
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8000

CMD sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"
