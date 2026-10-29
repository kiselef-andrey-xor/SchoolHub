# ---------- dev-стадия (горячая перезагрузка, используется корневым docker-compose) ----------
FROM node:20-alpine AS dev
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ---------- build-стадия (сборка статики) ----------
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
# относительный путь → запросы идут на тот же origin (через nginx-прокси), CORS не нужен
ARG VITE_API_URL=/api/v1
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

# ---------- prod-стадия: nginx + статика ----------
FROM nginx:1.27-alpine AS prod

# envsubst-шаблон: адрес backend и лимит загрузки берутся из ENV контейнера
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
# заголовки безопасности (CSP и пр.) — отдельным сниппетом, чтобы подключать их
# в каждом location со своим add_header (nginx не наследует их автоматически)
COPY security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html

ENV BACKEND_HOST=backend \
    BACKEND_PORT=8000 \
    MAX_UPLOAD_SIZE=30m \
    NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/conf.d

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=4s --retries=3 \
  CMD wget -qO- http://localhost/healthz >/dev/null 2>&1 || exit 1
