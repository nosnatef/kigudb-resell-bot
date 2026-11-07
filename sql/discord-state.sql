BEGIN;
-- CreateTable
CREATE TABLE "ResellPostTranslation" (
    "postId" TEXT NOT NULL,
    "sourceHash" TEXT NOT NULL,
    "model" TEXT NOT NULL,
    "englishTitle" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ResellPostTranslation_pkey" PRIMARY KEY ("postId")
);

-- CreateTable
CREATE TABLE "DiscordServer" (
    "guildId" TEXT NOT NULL,
    "channelId" TEXT NOT NULL,
    "enabled" BOOLEAN NOT NULL DEFAULT true,
    "latestPostId" TEXT,
    "latestMessageId" TEXT,
    "latestSentAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "DiscordServer_pkey" PRIMARY KEY ("guildId")
);

-- CreateTable
CREATE TABLE "DiscordDelivery" (
    "serverId" TEXT NOT NULL,
    "postId" TEXT NOT NULL,
    "channelId" TEXT NOT NULL,
    "reservedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "messageId" TEXT,
    "sentAt" TIMESTAMP(3),

    CONSTRAINT "DiscordDelivery_pkey" PRIMARY KEY ("serverId","postId")
);

-- CreateIndex
CREATE UNIQUE INDEX "DiscordServer_channelId_key" ON "DiscordServer"("channelId");

-- CreateIndex
CREATE INDEX "DiscordServer_latestPostId_idx" ON "DiscordServer"("latestPostId");

-- CreateIndex
CREATE INDEX "DiscordDelivery_postId_idx" ON "DiscordDelivery"("postId");

-- AddForeignKey
ALTER TABLE "ResellPostTranslation" ADD CONSTRAINT "ResellPostTranslation_postId_fkey" FOREIGN KEY ("postId") REFERENCES "ResellPost"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "DiscordServer" ADD CONSTRAINT "DiscordServer_latestPostId_fkey" FOREIGN KEY ("latestPostId") REFERENCES "ResellPost"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "DiscordDelivery" ADD CONSTRAINT "DiscordDelivery_serverId_fkey" FOREIGN KEY ("serverId") REFERENCES "DiscordServer"("guildId") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "DiscordDelivery" ADD CONSTRAINT "DiscordDelivery_postId_fkey" FOREIGN KEY ("postId") REFERENCES "ResellPost"("id") ON DELETE CASCADE ON UPDATE CASCADE;


COMMIT;
