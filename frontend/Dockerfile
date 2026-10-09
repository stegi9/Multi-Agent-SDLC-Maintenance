# Stage 1: build dell'applicazione Angular
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

# Stage 2: nginx serve la SPA e fa da reverse proxy verso i microservizi
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/sdlc-platform-frontend/browser /usr/share/nginx/html
EXPOSE 80
