FROM node:18-alpine AS deps

WORKDIR /app

COPY package*.json ./
RUN npm install

FROM node:18-alpine AS development

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

CMD ["npm", "run", "dev"]

FROM node:18-alpine AS build

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM nginx:stable-alpine AS production

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
