FROM python:3.11.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# eMudhra incident (Oct 2026): stop trusting the emSign roots Mozilla removed (ECC G3, ECC C3, C1); the
# build fails if any of them is still in the system store. Python's own bundle comes from certifi>=2026.10.23.
RUN sed -i -E 's#^(mozilla/emSign_(ECC_Root_CA_-_G3|ECC_Root_CA_-_C3|Root_CA_-_C1)\.crt)$#!\1#' /etc/ca-certificates.conf \
    && update-ca-certificates --fresh > /dev/null \
    && ! ls /etc/ssl/certs | grep -qE 'emSign_(ECC_Root_CA_-_(G3|C3)|Root_CA_-_C1)'

COPY . .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
