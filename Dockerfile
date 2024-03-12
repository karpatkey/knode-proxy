FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update -qq && apt-get upgrade -qq -y && \
    apt-get install -qq -y python3 wget

RUN wget -q https://bootstrap.pypa.io/get-pip.py && \
    python3 get-pip.py --break-system-packages

COPY . .

RUN pip install --break-system-packages -r requirements.txt

CMD ["uvicorn", "proxy:app", "--host=127.0.0.1", "--port=8888"]
