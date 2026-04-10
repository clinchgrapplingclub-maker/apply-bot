# Använd en Python-basimage (version 3.10)
FROM python:3.10-slim

# Sätt arbetsmappen i containern
WORKDIR /app

# Kopiera requirements.txt till containern
COPY requirements.txt .

# Installera python3-venv och uppdatera apt, ta bort onödiga filer efteråt
RUN apt-get update && apt-get install -y python3-venv && rm -rf /var/lib/apt/lists/*

# Skapa en virtuell miljö i /opt/venv
RUN python -m venv /opt/venv

# Lägg till venv bin i PATH så pip och python hittar rätt version
ENV PATH="/opt/venv/bin:$PATH"

# Uppgradera pip och installera beroenden
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Kopiera all kod från din dator till containern
COPY . .

# Kör din bot när containern startar
CMD ["python", "main.py"]
