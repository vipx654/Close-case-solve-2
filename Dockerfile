FROM python:3.10-slim

# ज़रूरी सिस्टम टूल्स इंस्टॉल करें
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /requirements.txt
RUN pip3 install --no-cache-dir -U pip && \
    pip3 install --no-cache-dir -U -r requirements.txt

RUN mkdir /TheMovieProviderBot
WORKDIR /TheMovieProviderBot
COPY . /TheMovieProviderBot
COPY start.sh /start.sh
CMD ["/bin/bash", "/start.sh"]
