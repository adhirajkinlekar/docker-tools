FROM node:20-alpine AS base

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --production && npm cache clean --force

FROM node:20-alpine AS development

WORKDIR /app
COPY --from=base /app/node_modules ./node_modules
COPY . .
ENV NODE_ENV=development
RUN npm install --only=development

CMD ["npm", "run", "dev"]

FROM node:20-alpine AS build

WORKDIR /app
COPY --from=development /app ./
RUN npm run build

FROM node:20-alpine AS production

WORKDIR /app
ENV NODE_ENV=production
COPY --from=base /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist

EXPOSE 3000
CMD ["node", "dist/index.js"]
