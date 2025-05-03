 # Install Tesseract and dependencies
 RUN apt-get update && apt-get install -y \
     tesseract-ocr \
     libtesseract-dev \
     && rm -rf /var/lib/apt/lists/*

 # Set working directory
 WORKDIR /app

 # Copy requirements and install
 COPY requirements.txt .
 RUN pip install --no-cache-dir -r requirements.txt

 # Copy application code
 COPY . .

 # Run Gunicorn
 CMD ["gunicorn", "whatsapp_money_transfer_mvp:app"]