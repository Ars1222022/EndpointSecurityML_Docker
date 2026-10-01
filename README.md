# EndpointSecurityML Docker

En stegvis, lokal MLOps-miljö för Windows, macOS och Linux. Docker Desktop eller Docker Engine med Compose v2 krävs. Kontrollpanelen behöver Python 3.10+; applikationerna i containrarna behöver ingen lokal Python-installation.

## Starta kontrollpanelen

Öppna en terminal i den här mappen och kör samma kommando på alla operativsystem:

```sh
python control-center/server.py
```

Öppna sedan <http://127.0.0.1:8765>. Kontrollpanelen startar och stoppar tjänster, visar status, beskriver stegen och länkar till apparna. Den körs lokalt och använder datorns Docker Compose.

## Workshop: kör MLOps steg för steg

Du kan använda knapparna i kontrollpanelen eller kommandona nedan. Börja med Docker Desktop/Engine startad.

1. **Starta API och PostgreSQL.**

	```sh
	docker compose up --build -d api
	```

	API-dokumentationen finns på <http://localhost:8000/docs>. PostgreSQL är tjänsten `postgres`; databasen heter `endpointsecurity`.

2. **Ladda exempeldata i databasen.** Klicka **Ladda exempeldata till PostgreSQL** i menyn. CSV-filen `data/raw/endpoint_activities.csv` importeras till tabellen `endpoint_activities`. Importen gör inget om tabellen redan innehåller rader.

3. **Spara en databasversion.** Klicka **Exportera DB-snapshot**. PostgreSQL skapar en komprimerad dump under `data/snapshots/`, och menyn lägger till den i DVC. Live-databasen finns i Docker-volymen `endpointsecurityml_postgres_data`; dumpfilen är en separat snapshot.

4. **Träna och jämför modeller.** Klicka **Kör träning** eller kör:

	```sh
	docker compose --profile training run --build --rm trainer
	```

	Träningen läser `endpoint_activities` från PostgreSQL, kontrollerar kolumner/etiketter och skapar processfeatures innan Random Forest tränas. Modellfiler sparas i `models/production/`. MLflow sparar varje körning och Accuracy, Precision, Recall och F1; resultaten visas i menyn. Tränade modeller läggs också till i DVC. Exempeldata är litet och syntetiskt: resultatet 1.000 visar att övningsflödet fungerar, inte verklig säkerhetsnoggrannhet.

5. **Versionera data och modeller.** Klicka **Versionera med DVC**. DVC skapar pekfiler och lokal cache i `.dvc/cache/`. Du kan även kontrollera läget med:

	```sh
	docker compose --profile dvc run --rm --no-deps dvc status
	```

	DVC-cache är lokal. Git-repository och DVC-remote är inte konfigurerade i denna arbetsyta. För att dela pekfiler mellan datorer, lägg dem i Git; för att dela själva data-/modellfilerna behöver ni dessutom ett gemensamt DVC-remote. DVC kör som root i sin isolerade container som standard för Windows/WSL; på Linux kan du sätta `DVC_UID` i `.env` till ditt användar-ID (`id -u`) för att äga skapade filer.

6. **Testa en prediktion.** Öppna Swagger på <http://localhost:8000/docs>, välj `POST /predict` och prova:

	```json
	{"NetworkConnections": 1, "ProcessName": "powershell.exe"}
	```

	Prediktionerna sparas i PostgreSQL-tabellen `prediction_events`.

7. **Lägg till Airflow och övervakning vid behov.**

	```sh
	docker compose --profile airflow up --build -d
	docker compose --profile monitoring up -d
	```

	I Airflow kör du DAG:en `endpointsecurity_train_model`. Efter en Airflow-körning väljer du **Versionera med DVC** för att lägga till den nya modellfilen. MLflow visar mätvärden för både manuella och Airflow-körningar.

Starta alla långlivade tjänster på en gång med:

```sh
docker compose --profile airflow --profile monitoring up --build -d api airflow-webserver airflow-scheduler prometheus grafana
```

## Appar och inloggningar
## Förväntat resultat

Efter steg 1 ska API och PostgreSQL visas som `running` i menyn. Efter steg 2 visar databasen omkring 35 exempelaktiviteter. Steg 3 skapar en `.dump`-fil under `data/snapshots/`. Steg 4 skapar en `.pkl`-modell, en MLflow-rad med Accuracy/Precision/Recall/F1 och en modellversion i DVC. Airflow-körningen ska stå som `success` i Airflow och synas som en ny rad i MLflow.

Exempeldata är liten och påhittad. Resultat på 1.000 visar bara att övningsflödet fungerar; det säger inte hur bra modellen upptäcker riktiga hot.

## Uppdateras menyn automatiskt?

Ja. Tjänstestatus hämtas ungefär var 5:e sekund och DB-, DVC- och MLflow-resultat ungefär var 10:e sekund. Du behöver normalt inte ladda om sidan. En ny träning via menyn visas när den är klar. En modell som tränats via Airflow visas också i listan, men klicka **Versionera med DVC** efter Airflow för att spara just den nya modellen i DVC.

Menyn tar ännu inte emot egna CSV-filer. **Ladda exempeldata** importerar den medföljande CSV:n bara när tabellen är tom; den skriver inte över befintliga rader. Att importera en ny dataversion måste därför göras separat innan nästa träning.

## Diskutrymme

Projektfilerna ligger där du har lagt mappen. I en mätning här var projektmappen cirka 1 MB. Docker lagrar däremot images, containers och databasvolymer i sin egen diskavbildning. På Windows med Docker Desktop ändrar du den under **Settings → Resources → Advanced → Disk image location** och väljer till exempel D:. Gör flytten i Docker Desktop och låt den flytta befintliga data; flytta inte diskavbildningen manuellt.

På den här datorn visade `docker system df` ungefär 8,3 GB images, 1,2 GB volymer och 11 GB byggcache, varav cirka 7,2 GB var möjlig att rensa. Det gäller hela Docker-motorn på datorn, inte bara den här appen. Images kan dela lager, så deras visade storlekar ska inte summeras rakt av. `docker system df` visar aktuell användning hos dig.

## Appar och inloggningar

- API och Swagger: <http://localhost:8000/docs>
- MLflow: <http://localhost:5000>
- Airflow: <http://localhost:8080>
- Prometheus: <http://localhost:9090>
- Grafana: <http://localhost:3000>
- PostgreSQL: `localhost:5432`, databas `endpointsecurity`

Alla appar som begär inloggning använder lokalt `admin` / `admin`. MLflow och API har ingen separat inloggning. Uppgifterna är bara för lokal testning; exponera inte tjänsterna på ett nätverk eller använd dessa lösenord i produktion.

Gör en testprediktion:

```sh
curl -X POST http://localhost:8000/predict -H "Content-Type: application/json" -d '{"NetworkConnections":1,"ProcessName":"powershell.exe"}'
```

Prediktioner sparas i PostgreSQL-tabellen `prediction_events`. Träningsfiler ska ligga i `data/raw/` och innehålla kolumnerna `NetworkConnections`, `ProcessName` och `IsAttack`.

## GitHub Actions och Docker Hub

Workflows finns i `.github/workflows/`. CI kör API-testerna. När du skapar GitHub-repositoryt ska innehållet i mappen `EndpointSecurityML_Docker` ligga direkt i repositoryts rot. När du öppnar repositoryt på GitHub ska du alltså se `compose.yaml` och mappen `.github` direkt där; ladda inte upp den överordnade projektmappen.

Publiceringsflödet behöver två repository-secrets med exakt dessa namn: `DOCKER_USERNAME` och `DOCKER_PASSWORD`. Värdet för `DOCKER_PASSWORD` ska vara en Docker Hub access token, inte ditt Docker Hub-kontolösenord. GitHub tillhandahåller automatiskt `GITHUB_TOKEN` för workflowen; skapa eller klistra inte in en GitHub PAT.

Lägg till dem manuellt i GitHub: öppna repositoryt, välj **Settings → Secrets and variables → Actions → New repository secret**, skapa `DOCKER_USERNAME` med ditt Docker Hub-användarnamn och `DOCKER_PASSWORD` med access token.

Eller ange repository som `ägare/repo` och klistra in Docker Hub-uppgifterna i kontrollpanelen. Då måste GitHub CLI vara installerat och inloggat. Värdena skickas till `gh`, visas inte igen och sparas inte av kontrollpanelen eller webbläsaren:

```sh
gh auth login
gh auth status
```

Skapa en Docker Hub-token på <https://hub.docker.com/settings/security>. Secrets kan också sättas manuellt på `https://github.com/OWNER/REPO/settings/secrets/actions` (byt ut `OWNER/REPO` mot ditt repository).

## PostgreSQL, inte SQL Server

Den portabla Docker-appen använder PostgreSQL 16. `postgres/init/01-schema.sql` är PostgreSQL-SQL, inte T-SQL: den skapar `endpoint_activities`, importerar exempel-CSV:n och skapar `prediction_events`. `CREATE DATABASE airflow OWNER admin` skapar Airflows separata metadata-databas; tabellerna i resten av filen skapas i `endpointsecurity`.

VS Codes SQL Server/T-SQL-parser kan därför markera `OWNER`, `BIGSERIAL`, `TIMESTAMPTZ` eller `COPY` som fel. Det är dialektvarningar, inte PostgreSQL-körfel. Använd PostgreSQL-dialekt/stöd i editorn eller kontrollera körningen i PostgreSQL-loggen. Initierings-SQL körs automatiskt bara när en ny Postgres-volym skapas. Menyknappen **Ladda exempeldata till PostgreSQL** skapar/importerar motsvarande tabell idempotent även i en redan befintlig volym.

## Data och felsökning

```sh
docker compose ps -a
docker compose logs -f api
docker compose logs -f postgres
docker compose stop
docker compose down
```

`airflow-init` är en engångscontainer. `Exited (0)` betyder att initieringen lyckades; den ska inte stå och köra. `docker compose down` behåller data. `docker compose down -v` raderar databas, modeller, Grafana-inställningar och MLflow-körningar.

`01-schema.sql` är PostgreSQL 16 och körs automatiskt bara när Postgres-volymen skapas första gången. SQL-filen i `EndpointSecurityML_Skript` är T-SQL för SQL Server och körs inte i den portabla Compose-miljön.

För andra lokala lösenord, kopiera `.env.example` till `.env` och ändra före första start. Befintliga databas- och Grafana-volymer behåller redan initierade konton.

Kör API-testerna:

```sh
docker compose run --build --rm --no-deps api pytest -q
```