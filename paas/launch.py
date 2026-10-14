"""`aloof launch`: detect the framework of a source tree and generate a Dockerfile + aloof.toml. Builds without a Dockerfile use
Nixpacks when the `nixpacks` binary is installed on the build host, Cloud Native Buildpacks (`pack`) when that is, and otherwise
the Dockerfile generated here. Detection reads only file names and a few manifests (package.json, pyproject, Gemfile, ...)."""
import os, re, json, shutil, subprocess
KEY_FILES = ["package.json", "requirements.txt", "pyproject.toml", "Pipfile", "manage.py", "go.mod", "Cargo.toml", "Gemfile", "composer.json",
             "pom.xml", "build.gradle", "build.gradle.kts", "mix.exs", "index.html", "Procfile", "main.py", "app.py", "Dockerfile", "bun.lockb",
             "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "artisan", "config.ru", "gradlew", "mvnw", ".python-version", ".nvmrc"]
def _has(files, *names): return any(n in files for n in names)
def _py_app(files):
    blob = " ".join((files.get(k) or "") for k in ("requirements.txt", "pyproject.toml", "Pipfile")).lower()
    entry = "main" if "main.py" in files else "app"
    src = files.get(f"{entry}.py") or ""
    if "manage.py" in files:
        proj = (re.search(r"['\"]([\w]+)\.settings['\"]", files.get("manage.py") or "") or [None, "config"])[1]
        return "django", f'["sh","-c","gunicorn {proj}.wsgi --bind 0.0.0.0:$PORT"]', "python manage.py migrate --noinput", ["gunicorn"]
    if "fastapi" in blob or "FastAPI(" in src: return "fastapi", f'["sh","-c","uvicorn {entry}:app --host 0.0.0.0 --port $PORT"]', None, ["uvicorn"]
    if "flask" in blob or "Flask(" in src: return "flask", f'["sh","-c","gunicorn {entry}:app --bind 0.0.0.0:$PORT"]', None, ["gunicorn"]
    return "python", f'["python","{entry}.py"]', None, []
def detect(files):
    """files: {name: content-or-None} for the top-level entries (content for KEY_FILES that exist). Returns a launch plan."""
    if "Dockerfile" in files: return {"framework": "dockerfile", "dockerfile": None, "port": _expose(files.get("Dockerfile") or "") or 8080, "release_command": None}
    pj = {}
    if "package.json" in files:
        try: pj = json.loads(files.get("package.json") or "{}")
        except Exception: pj = {}
    deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})} if isinstance(pj, dict) else {}
    if "package.json" in files:
        fw = "nextjs" if "next" in deps else "remix" if any(k.startswith("@remix-run") for k in deps) else "nuxt" if "nuxt" in deps else "node"
        pm = "pnpm" if "pnpm-lock.yaml" in files else "yarn" if "yarn.lock" in files else "bun" if "bun.lockb" in files else "npm"
        inst = {"npm": "npm ci || npm install", "yarn": "corepack enable && yarn install --frozen-lockfile || yarn install", "pnpm": "corepack enable && pnpm install --frozen-lockfile",
                "bun": "npm i -g bun && bun install"}[pm]
        run = {"npm": "npm", "yarn": "yarn", "pnpm": "pnpm", "bun": "bun"}[pm]
        start = f'["{run}","run","start"]' if (pj.get("scripts") or {}).get("start") else '["node","' + (pj.get("main") or "index.js") + '"]'
        build = f"RUN {run} run build\n" if (pj.get("scripts") or {}).get("build") else ""
        df = f"""FROM node:20-slim
WORKDIR /app
ENV NODE_ENV=production PORT=8080 HOSTNAME=0.0.0.0
COPY . .
RUN NODE_ENV=development {inst}
{build}EXPOSE 8080
CMD {start}
"""
        return {"framework": fw, "dockerfile": df, "port": 8080, "release_command": None}
    if _has(files, "requirements.txt", "pyproject.toml", "Pipfile", "manage.py", "main.py", "app.py") and not _has(files, "Gemfile", "composer.json", "go.mod"):
        fw, cmd, rel, extra = _py_app(files)
        if "requirements.txt" in files: inst = "pip install --no-cache-dir -r requirements.txt"
        elif "pyproject.toml" in files: inst = "pip install --no-cache-dir ."
        elif "Pipfile" in files: inst = "pip install --no-cache-dir pipenv && pipenv install --system --deploy"
        else: inst = "true"
        df = f"""FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8080
COPY . .
RUN {inst}{(' && pip install --no-cache-dir ' + ' '.join(extra)) if extra else ''}
EXPOSE 8080
CMD {cmd}
"""
        return {"framework": fw, "dockerfile": df, "port": 8080, "release_command": rel}
    if "go.mod" in files:
        df = """FROM golang:1.23 AS build
WORKDIR /src
COPY . .
RUN go mod download && CGO_ENABLED=0 go build -o /out/app .
FROM gcr.io/distroless/static-debian12
COPY --from=build /out/app /app
ENV PORT=8080
EXPOSE 8080
CMD ["/app"]
"""
        return {"framework": "go", "dockerfile": df, "port": 8080, "release_command": None}
    if "Cargo.toml" in files:
        name = (re.search(r'^\s*name\s*=\s*"([^"]+)"', files.get("Cargo.toml") or "", re.M) or [None, "app"])[1]
        df = f"""FROM rust:1 AS build
WORKDIR /src
COPY . .
RUN cargo build --release && cp target/release/{name} /app
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /app /app
ENV PORT=8080
EXPOSE 8080
CMD ["/app"]
"""
        return {"framework": "rust", "dockerfile": df, "port": 8080, "release_command": None}
    if "Gemfile" in files:
        rails = "rails" in (files.get("Gemfile") or "")
        cmd = '["sh","-c","bin/rails server -b 0.0.0.0 -p $PORT"]' if rails else '["sh","-c","bundle exec rackup -o 0.0.0.0 -p $PORT"]'
        df = f"""FROM ruby:3.3-slim
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git libpq-dev libyaml-dev nodejs && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV RAILS_ENV=production RACK_ENV=production BUNDLE_WITHOUT=development:test RAILS_LOG_TO_STDOUT=1 RAILS_SERVE_STATIC_FILES=1 PORT=8080
COPY . .
RUN bundle install{' && (SECRET_KEY_BASE_DUMMY=1 bin/rails assets:precompile || true)' if rails else ''}
EXPOSE 8080
CMD {cmd}
"""
        return {"framework": "rails" if rails else "ruby", "dockerfile": df, "port": 8080, "release_command": "bin/rails db:migrate" if rails else None}
    if "composer.json" in files or any(n.endswith(".php") for n in files):
        lar = "artisan" in files
        cmd = '["sh","-c","php artisan serve --host 0.0.0.0 --port $PORT"]' if lar else ('["sh","-c","php -S 0.0.0.0:$PORT -t public"]' if "public" in files else '["sh","-c","php -S 0.0.0.0:$PORT"]')
        df = f"""FROM php:8.3-cli
RUN apt-get update && apt-get install -y --no-install-recommends git unzip libzip-dev libpq-dev && docker-php-ext-install zip pdo_mysql pdo_pgsql && rm -rf /var/lib/apt/lists/*
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
WORKDIR /app
ENV PORT=8080
COPY . .
RUN if [ -f composer.json ]; then composer install --no-dev --optimize-autoloader --no-interaction; fi
EXPOSE 8080
CMD {cmd}
"""
        return {"framework": "laravel" if lar else "php", "dockerfile": df, "port": 8080, "release_command": "php artisan migrate --force" if lar else None}
    if _has(files, "pom.xml", "build.gradle", "build.gradle.kts"):
        if "pom.xml" in files: b = "FROM maven:3-eclipse-temurin-21 AS build\nWORKDIR /src\nCOPY . .\nRUN mvn -q -DskipTests package && cp $(ls target/*.jar | grep -v original | head -1) /app.jar\n"
        else: b = "FROM gradle:8-jdk21 AS build\nWORKDIR /src\nCOPY . .\nRUN gradle --no-daemon -q build -x test && cp $(ls build/libs/*.jar | grep -v plain | head -1) /app.jar\n"
        df = b + """FROM eclipse-temurin:21-jre
COPY --from=build /app.jar /app.jar
ENV PORT=8080 SERVER_PORT=8080
EXPOSE 8080
CMD ["sh","-c","java -Dserver.port=$PORT -jar /app.jar"]
"""
        return {"framework": "java", "dockerfile": df, "port": 8080, "release_command": None}
    if "mix.exs" in files:
        phx = "phoenix" in (files.get("mix.exs") or "")
        df = f"""FROM elixir:1.17
WORKDIR /app
ENV MIX_ENV=prod PORT=8080 PHX_SERVER=true
RUN mix local.hex --force && mix local.rebar --force
COPY . .
RUN mix deps.get --only prod && mix compile{' && (mix assets.deploy || true)' if phx else ''}
EXPOSE 8080
CMD {'["mix","phx.server"]' if phx else '["mix","run","--no-halt"]'}
"""
        return {"framework": "phoenix" if phx else "elixir", "dockerfile": df, "port": 8080, "release_command": "mix ecto.migrate" if phx else None,
                "notes": ["Phoenix needs SECRET_KEY_BASE: aloof secrets set SECRET_KEY_BASE=$(mix phx.gen.secret)"] if phx else []}
    if "index.html" in files or "public" in files or "dist" in files:
        root = "." if "index.html" in files else ("dist" if "dist" in files else "public")
        df = f"""FROM nginx:alpine
COPY {root} /usr/share/nginx/html
RUN sed -i 's/listen  *80;/listen 8080;/; s/listen  *\\[::\\]:80;/listen [::]:8080;/' /etc/nginx/conf.d/default.conf
EXPOSE 8080
"""
        return {"framework": "static", "dockerfile": df, "port": 8080, "release_command": None}
    return {"framework": None, "dockerfile": None, "port": 8080, "release_command": None}
def _expose(df):
    m = re.search(r"^\s*EXPOSE\s+(\d+)", df, re.M | re.I); return int(m.group(1)) if m else None
def scan_dir(path):
    names = os.listdir(path); files = {}
    for n in names:
        files[n] = None
        if n in KEY_FILES and os.path.isfile(os.path.join(path, n)):
            try: files[n] = open(os.path.join(path, n), errors="replace").read(200_000)
            except OSError: pass
    return files
def toml_for(app, plan, framework):
    rel = f'\n[deploy]\n  release_command = "{plan["release_command"]}"\n' if plan.get("release_command") else ""
    return f"""# aloof.toml (generated by `aloof launch`, framework: {framework or 'unknown'}). Reference: https://www.aloof.si/docs/config
app = "{app}"
primary_region = "nbg1"

[build]
  dockerfile = "Dockerfile"

[env]
  PORT = "{plan.get('port') or 8080}"

[http_service]
  internal_port = {plan.get('port') or 8080}
  force_https = true
  auto_stop_machines = true
  min_machines_running = 0

[[http_service.checks]]
  path = "/"
  interval = "10s"
{rel}
[[vm]]
  size = "shared-1x-256mb"   # free tier; bigger: shared-1x-1gb, shared-2x-2gb, perf-2x-4gb ...
"""
def prepare_context(ctx, log, tag=None, env=None):
    """Make ctx buildable. Returns None (Dockerfile present/generated) or an image tag built by nixpacks/pack."""
    if os.path.exists(os.path.join(ctx, "Dockerfile")): return None
    if shutil.which("nixpacks") and tag and os.environ.get("PAAS_NO_NIXPACKS") != "1":
        log("no Dockerfile: building with Nixpacks")
        p = subprocess.run(["nixpacks", "build", ctx, "--name", tag], capture_output=True, text=True, env=env, timeout=3600)
        for line in (p.stdout + p.stderr).splitlines()[-30:]: log(line)
        if p.returncode == 0: return tag
        log("nixpacks failed; falling back to a generated Dockerfile")
    if shutil.which("pack") and tag and os.environ.get("PAAS_NO_BUILDPACKS") != "1":
        log("no Dockerfile: building with Cloud Native Buildpacks (heroku/builder:24)")
        p = subprocess.run(["pack", "build", tag, "--path", ctx, "--builder", "heroku/builder:24", "--trust-builder"], capture_output=True, text=True, env=env, timeout=3600)
        for line in (p.stdout + p.stderr).splitlines()[-30:]: log(line)
        if p.returncode == 0: return tag
        log("buildpacks failed; falling back to a generated Dockerfile")
    plan = detect(scan_dir(ctx))
    if not plan["dockerfile"]: raise RuntimeError("no Dockerfile and the framework could not be detected; run `aloof launch` or add a Dockerfile")
    open(os.path.join(ctx, "Dockerfile"), "w").write(plan["dockerfile"]); log(f"no Dockerfile: generated one for {plan['framework']}")
    return None
