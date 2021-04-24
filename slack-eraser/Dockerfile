FROM python:alpine

RUN apk add --no-cache bash curl \
	&& pip3 install slacker

ADD slack-eraser.py /

CMD ["python", "/slack-eraser.py"]
