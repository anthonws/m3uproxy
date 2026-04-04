FROM python:3.12-alpine
WORKDIR /app
COPY proxy.py .
CMD ["python", "-u", "proxy.py"]
