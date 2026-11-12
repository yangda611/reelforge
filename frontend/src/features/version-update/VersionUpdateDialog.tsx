// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { subscribeOpenVersionUpdateDialog } from "@/features/version-update/version-update-events";
import {
  ensureReleaseNotifications,
  normalizeReleaseLocale,
  useReleaseNotifications,
} from "@/lib/queries/release-notifications";
import {
  markCurrentReleaseSeen,
} from "@/lib/release-notification-state";

/**
 * 首次运行的产品简介（localStorage 一次性标记）。
 * P2-1：首次运行不再弹更新日志，改成产品简介/快速开始；更新日志只进
 * 公告中心（头像菜单 → 公告中心，小红点提示未读），本弹窗只响应那里的
 * 打开事件。
 */
const PRODUCT_INTRO_SEEN_KEY = "reelforge:product-intro-seen";

function productIntroSeen(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(PRODUCT_INTRO_SEEN_KEY) === "seen";
  } catch {
    return true;
  }
}

function markProductIntroSeen(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PRODUCT_INTRO_SEEN_KEY, "seen");
  } catch {
    // ignore
  }
}

export function VersionUpdateDialog() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const locale = normalizeReleaseLocale(i18n.resolvedLanguage ?? i18n.language);
  const releaseNotifications = useReleaseNotifications(locale);
  const feed = releaseNotifications.data?.data;
  const items = feed?.current_items ?? [];
  const [open, setOpen] = useState(false);
  const [introOpen, setIntroOpen] = useState(false);
  const introCheckedRef = useRef(false);

  // 首次运行：只弹产品简介，不弹更新日志。看完即记为已见，不再打扰。
  useEffect(() => {
    if (introCheckedRef.current) return;
    introCheckedRef.current = true;
    if (!productIntroSeen()) {
      setIntroOpen(true);
    }
  }, []);

  const closeIntro = () => {
    markProductIntroSeen();
    setIntroOpen(false);
  };

  useEffect(
    () =>
      subscribeOpenVersionUpdateDialog(() => {
        void ensureReleaseNotifications(queryClient, locale).finally(() => setOpen(true));
      }),
    [locale, queryClient],
  );

  // 从公告中心打开更新日志时，把当前版本记为已见（打开时才记，而不是事件到
  // 达时——事件到达时 feed 可能还没加载完）。
  useEffect(() => {
    if (open && feed?.current_tag) markCurrentReleaseSeen(feed.current_tag);
  }, [open, feed?.current_tag]);

  return (
    <>
    <Dialog open={introOpen} onOpenChange={(next) => { if (!next) closeIntro(); }}>
      <DialogContent
        showCloseButton={false}
        overlayClassName="bg-black/56 backdrop-blur-md supports-backdrop-filter:backdrop-blur-md"
        className="max-h-[min(84dvh,560px)] w-[min(calc(100vw-32px),360px)] gap-0 overflow-hidden rounded-[14px] border-0 bg-white p-0 text-slate-950 shadow-[0_16px_48px_rgba(0,0,0,0.26)] ring-0 sm:max-w-[360px]"
      >
        <div className="p-2">
          <div className="relative flex aspect-[2/1] flex-col items-start justify-end overflow-hidden rounded-[12px] bg-gradient-to-br from-[#0f2027] via-[#203a43] to-[#2c5364] p-4">
            <p className="text-[22px] font-bold leading-none tracking-tight text-white">ReelForge</p>
            <p className="mt-1.5 text-[12px] leading-5 text-white/75">把小说或想法变成短剧</p>
          </div>
        </div>

        <div className="px-4.5 pb-5 pt-3.5 sm:px-5">
          <DialogTitle className="text-[17px] font-medium leading-tight tracking-normal text-slate-950 sm:text-[18px]">
            {t("app.versionUpdate.introTitle")}
          </DialogTitle>
          <p className="mt-2 text-[12.5px] leading-6 text-slate-700 sm:text-[13.5px]">
            {t("app.versionUpdate.introBody")}
          </p>
          <ol className="mt-4 space-y-2 text-[12.5px] leading-6 text-slate-700 sm:text-[13.5px]">
            <li>1. {t("app.versionUpdate.introStep1")}</li>
            <li>2. {t("app.versionUpdate.introStep2")}</li>
            <li>3. {t("app.versionUpdate.introStep3")}</li>
          </ol>
          <Button
            type="button"
            className="mt-7 h-10 w-full rounded-[8px] bg-neutral-950 text-[14px] font-medium text-white shadow-none transition-[background,box-shadow,filter] duration-200 ease-out hover:bg-[#171717] hover:shadow-[0_0_0_1px_rgba(255,255,255,0.08)_inset,0_7px_18px_rgba(0,0,0,0.16)] hover:brightness-110 focus-visible:border-transparent focus-visible:ring-0 active:bg-neutral-950 active:shadow-[0_0_0_1px_rgba(255,255,255,0.05)_inset]"
            onClick={closeIntro}
          >
            {t("app.versionUpdate.introStart")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        showCloseButton={false}
        overlayClassName="bg-black/56 backdrop-blur-md supports-backdrop-filter:backdrop-blur-md"
        className="max-h-[min(84dvh,560px)] w-[min(calc(100vw-32px),360px)] gap-0 overflow-hidden rounded-[14px] border-0 bg-white p-0 text-slate-950 shadow-[0_16px_48px_rgba(0,0,0,0.26)] ring-0 sm:max-w-[360px]"
      >
        <div className="p-2">
          {/* P2-1：旧的纯色头图看着像一张坏掉的图片，改成品牌渐变 + 标题，不再留空白图位。 */}
          <div className="relative flex aspect-[2/1] flex-col items-start justify-end overflow-hidden rounded-[12px] bg-gradient-to-br from-[#0f2027] via-[#203a43] to-[#2c5364] p-4">
            <p className="text-[22px] font-bold leading-none tracking-tight text-white">ReelForge</p>
            <p className="mt-1.5 text-[12px] leading-5 text-white/75">
              {t("app.versionUpdate.title")}
            </p>
          </div>
        </div>

        <div className="px-4.5 pb-5 pt-3.5 sm:px-5">
          <DialogTitle className="text-[17px] font-medium leading-tight tracking-normal text-slate-950 sm:text-[18px]">
            {t("app.versionUpdate.title")}
          </DialogTitle>
          <div className="mt-4 max-h-[138px] space-y-4 overflow-y-auto pr-2 text-[12.5px] leading-6 text-slate-700 [scrollbar-gutter:stable] sm:text-[13.5px]">
            {items.length > 0 ? (
              items.map((item, index) => (
                <p key={item.id} className="m-0">
                  {index + 1}. {item.title}
                  {item.body ? `: ${item.body}` : ""}
                </p>
              ))
            ) : (
              <p className="m-0">{t("app.versionUpdate.empty")}</p>
            )}
          </div>
          <Button
            type="button"
            className="mt-7 h-10 w-full rounded-[8px] bg-neutral-950 text-[14px] font-medium text-white shadow-none transition-[background,box-shadow,filter] duration-200 ease-out hover:bg-[#171717] hover:shadow-[0_0_0_1px_rgba(255,255,255,0.08)_inset,0_7px_18px_rgba(0,0,0,0.16)] hover:brightness-110 focus-visible:border-transparent focus-visible:ring-0 active:bg-neutral-950 active:shadow-[0_0_0_1px_rgba(255,255,255,0.05)_inset]"
            onClick={() => setOpen(false)}
          >
            {t("app.versionUpdate.confirm")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
    </>
  );
}
