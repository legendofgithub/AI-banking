/*
 * 本地 SQLite 建表语句（幂等）。
 * 与 schema.ts 一一对应；时间列统一 epoch 毫秒整数，布尔列 0/1，json 列存文本。
 * 只在首次连接时执行一次（见 client.ts），不做版本迁移——演示库删档即重建。
 */
export const SCHEMA_DDL = `
CREATE TABLE IF NOT EXISTS "User" (
  "createdAt" integer NOT NULL,
  "email" text NOT NULL,
  "emailVerified" integer DEFAULT 0 NOT NULL,
  "id" text PRIMARY KEY NOT NULL,
  "image" text,
  "isAnonymous" integer DEFAULT 0 NOT NULL,
  "name" text,
  "password" text,
  "updatedAt" integer NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS "User_email_idx" ON "User" ("email");

CREATE TABLE IF NOT EXISTS "Chat" (
  "createdAt" integer NOT NULL,
  "id" text PRIMARY KEY NOT NULL,
  "title" text NOT NULL,
  "userId" text NOT NULL,
  "visibility" text DEFAULT 'private' NOT NULL,
  FOREIGN KEY ("userId") REFERENCES "User" ("id")
);
CREATE INDEX IF NOT EXISTS "Chat_userId_idx" ON "Chat" ("userId");

CREATE TABLE IF NOT EXISTS "Message_v2" (
  "attachments" text NOT NULL,
  "chatId" text NOT NULL,
  "createdAt" integer NOT NULL,
  "id" text PRIMARY KEY NOT NULL,
  "parts" text NOT NULL,
  "role" text NOT NULL,
  FOREIGN KEY ("chatId") REFERENCES "Chat" ("id")
);
CREATE INDEX IF NOT EXISTS "Message_v2_chatId_idx" ON "Message_v2" ("chatId");

CREATE TABLE IF NOT EXISTS "Vote_v2" (
  "chatId" text NOT NULL,
  "isUpvoted" integer NOT NULL,
  "messageId" text NOT NULL,
  PRIMARY KEY ("chatId", "messageId"),
  FOREIGN KEY ("chatId") REFERENCES "Chat" ("id"),
  FOREIGN KEY ("messageId") REFERENCES "Message_v2" ("id")
);

CREATE TABLE IF NOT EXISTS "Document" (
  "content" text,
  "createdAt" integer NOT NULL,
  "id" text NOT NULL,
  "kind" text DEFAULT 'text' NOT NULL,
  "title" text NOT NULL,
  "userId" text NOT NULL,
  PRIMARY KEY ("id", "createdAt"),
  FOREIGN KEY ("userId") REFERENCES "User" ("id")
);

CREATE TABLE IF NOT EXISTS "Suggestion" (
  "createdAt" integer NOT NULL,
  "description" text,
  "documentCreatedAt" integer NOT NULL,
  "documentId" text NOT NULL,
  "id" text PRIMARY KEY NOT NULL,
  "isResolved" integer DEFAULT 0 NOT NULL,
  "originalText" text NOT NULL,
  "suggestedText" text NOT NULL,
  "userId" text NOT NULL,
  FOREIGN KEY ("userId") REFERENCES "User" ("id"),
  FOREIGN KEY ("documentId", "documentCreatedAt") REFERENCES "Document" ("id", "createdAt")
);

CREATE TABLE IF NOT EXISTS "Stream" (
  "chatId" text NOT NULL,
  "createdAt" integer NOT NULL,
  "id" text PRIMARY KEY NOT NULL,
  FOREIGN KEY ("chatId") REFERENCES "Chat" ("id")
);
CREATE INDEX IF NOT EXISTS "Stream_chatId_idx" ON "Stream" ("chatId");
`;
