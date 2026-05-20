# parkering-booking

Book plass 44 – enkel korttidsparkering.

## Kjøre lokalt

```bash
uvicorn main:app --reload
```

Åpne http://127.0.0.1:8000

## Admin (se og slette bookinger)

1. Sett miljøvariabel `ADMIN_PASSWORD` (velg et sterkt passord).
2. Gå til `/admin` og logg inn.

### Lokalt (PowerShell)

```powershell
$env:ADMIN_PASSWORD = "ditt-hemmelige-passord"
uvicorn main:app --reload
```

### Render

1. Dashboard → din Web Service → **Environment**
2. Legg til: `ADMIN_PASSWORD` = ditt passord
3. **Save Changes** (appen starter på nytt)

Admin-URL: `https://din-app.onrender.com/admin`

Valgfritt: `ADMIN_SECRET` for signering av innloggings-cookie (ellers brukes passordet).
