FROM python:3.12-slim
WORKDIR /app
COPY server.py .
EXPOSE 9000 9001
# -u 让 print 实时输出到 docker logs
CMD ["python", "-u", "server.py"]
