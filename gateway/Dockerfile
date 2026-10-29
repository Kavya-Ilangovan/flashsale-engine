FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN node build.js

FROM nginx:1.27-alpine
COPY gateway/nginx.conf /etc/nginx/nginx.conf
COPY gateway/proxy_common.conf /etc/nginx/proxy_common.conf
COPY gateway/loadtest-allow.conf /etc/nginx/loadtest-allow.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
