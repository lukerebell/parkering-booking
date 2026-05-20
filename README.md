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

## Bookinger forsvinner ved deploy på Render?

På **Free** lagres `bookings.db` på midlertidig disk – filen **slettes ved ny deploy**.

### Løsning A: Persistent Disk (enklest med SQLite)

Krever **Starter**-plan (samme som gir Shell).

1. Render → Web Service → **Disks** → **Add disk**
   - Mount path: `/var/data`
   - Size: 1 GB (holder mer enn nok)
2. **Environment** → legg til:
   - `DATABASE_PATH` = `/var/data/bookings.db`
3. Deploy på nytt

Da ligger databasen på disken som overlever deploy.

### Løsning B: Ekstern database (PostgreSQL)

Gratis nivå hos f.eks. [Neon](https://neon.tech) eller Supabase – data overlever alltid uavhengig av Render-plan. Krever kodeendring (si fra om du vil ha det satt opp).

### Lokalt

Uten `DATABASE_PATH` brukes `bookings.db` i prosjektmappen som før.
