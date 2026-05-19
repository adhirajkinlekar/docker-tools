FROM node:20-alpine

WORKDIR /usr/src/app

COPY package*.json ./

RUN npm install --production && npm cache clean --force

COPY . .

ENV NODE_ENV=production

EXPOSE 3000

USER node

CMD ["npm", "start"]
