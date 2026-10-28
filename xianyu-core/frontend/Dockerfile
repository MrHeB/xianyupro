FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
# 前端静态站，API 经 nginx 反代到 backend 服务
COPY nginx.conf /etc/nginx/conf.d/default.conf
