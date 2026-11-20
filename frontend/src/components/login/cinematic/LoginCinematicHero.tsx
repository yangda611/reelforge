import { useEffect, useState } from "react";
import { ChevronDown, Download, Mouse } from "lucide-react";
import type { CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { Brand } from "@/components/login/login-stage";
import Aurora from "@/components/react-bits/aurora";
import SplitText from "@/components/react-bits/split-text";
import { AppleMark, WindowsMark } from "@/components/platform-marks";
import { useDesktopRelease } from "@/hooks/use-desktop-release";
import { detectDesktopPlatform, type DesktopPlatform } from "@/lib/desktop-download";
import styles from "@/components/login/login.module.css";
import layout from "./hero-layout.module.css";
import { MoreInfoMenu } from "./MoreInfoMenu";

// 桌面端下载入口暂时隐藏；组件与样式全部保留，改回 true 即可恢复。
const SHOW_DESKTOP_DOWNLOAD = false;

/**
 * Installer downloads, in the header next to 商务联系 / Star. The detected
 * platform is listed first so the common case is the first thing under the
 * cursor, but both stay one click away — no OS sniffing dead-ends.
 */
function DesktopDownload() {
  const { t } = useTranslation();
  const [platform, setPlatform] = useState<DesktopPlatform>("mac");
  const releases = useDesktopRelease();

  // Detect after mount so a cached/prerendered shell can't bake in the wrong
  // platform's ordering.
  useEffect(() => {
    setPlatform(detectDesktopPlatform());
  }, []);

  const ordered: DesktopPlatform[] =
    platform === "windows" ? ["windows", "mac"] : ["mac", "windows"];

  return (
    <div className={styles.desktopDownload}>
      <button
        type="button"
        className={styles.desktopDownloadTrigger}
        aria-label={t("auth.download.aria")}
      >
        <Download aria-hidden="true" />
        {t("auth.download.label")}
        <ChevronDown className={styles.desktopDownloadCaret} aria-hidden="true" />
      </button>
      <div
        className={styles.desktopDownloadPopover}
        role="dialog"
        aria-label={t("auth.download.aria")}
      >
        <div className={styles.desktopDownloadPanel}>
          {ordered.map((os) => {
            const Mark = os === "mac" ? AppleMark : WindowsMark;
            return (
              <a
                key={os}
                className={styles.desktopDownloadItem}
                href={releases[os].url}
                download
              >
                <Mark />
                {t(`auth.download.primary.${os}`)}
              </a>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export function LoginCinematicHeader({
  className,
  style,
}: {
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={`${styles.stageTopBar} ${className ?? ""}`}
      style={style}
    >
      <Brand />
      <div className={styles.stageActions}>
        {/* 桌面端下载入口暂时隐藏，改回 true 即可恢复（组件代码保留）。 */}
        {SHOW_DESKTOP_DOWNLOAD && <DesktopDownload />}
        <MoreInfoMenu />
      </div>
    </div>
  );
}

export function LoginCinematicHero({
  heroExitProgress,
  onStart,
}: {
  heroExitProgress: number;
  onStart: () => void;
}) {
  const { t } = useTranslation();
  const scrollCueExitStyle =
    heroExitProgress > 0.002
      ? ({
          opacity: Math.max(0, 1 - heroExitProgress * 7),
          filter: `blur(${heroExitProgress * 8}px)`,
        } satisfies CSSProperties)
      : undefined;
  const headerStyle =
    heroExitProgress > 0.002
      ? ({
          "--stage-header-opacity": Math.max(0, 1 - heroExitProgress * 3.2),
          "--stage-header-offset": `${heroExitProgress * -18}px`,
          filter: `blur(${heroExitProgress * 8}px)`,
          pointerEvents: heroExitProgress < 0.22 ? "auto" : "none",
        } as CSSProperties)
      : undefined;

  return (
    <>
      <Aurora
        className={layout.heroAurora}
        colorStops={["#06B6D4", "#A855F7", "#5227FF"]}
        speed={0.5}
      />

      <div className={`${styles.stageInner} ${layout.stageInner}`}>
        <LoginCinematicHeader style={headerStyle} />

        <div className={`${styles.hero} ${layout.hero}`}>
          <SplitText
            tag="h1"
            text={t("auth.stage.headlines.createUniverse")}
            className={`${styles.heroTitle} ${layout.heroTitle}`}
            delay={70}
            duration={0.8}
            ease="power3.out"
            splitType="chars"
            from={{ opacity: 0, y: 36 }}
            to={{ opacity: 1, y: 0 }}
            threshold={0.1}
            rootMargin="-100px"
            textAlign="center"
            initiallyHidden
          />
          <p className={`${styles.heroSubtitle} ${layout.heroSubtitle}`}>
            <span className={styles.heroSubtitlePrefix}>
              {t("auth.stage.subtitlePrefix")}
            </span>
            <span className={styles.heroSubtitleBrand}>
              {t("auth.stage.subtitleBrand")}
            </span>
            <span className={styles.heroSubtitleSuffix}>
              {t("auth.stage.subtitleSuffix")}
            </span>
          </p>
          <div className={`${styles.heroActions} ${layout.heroActions}`}>
            <button
              type="button"
              className={`${styles.heroPrimary} ${layout.heroPrimary}`}
              onClick={onStart}
            >
              {t("loginCinematic.hero.headline")}
            </button>
          </div>
        </div>

        <div
          className={layout.scrollCue}
          style={scrollCueExitStyle}
          aria-hidden="true"
        >
          <div className={layout.scrollCueInner}>
            <Mouse className={layout.scrollMouseIcon} />
            <span>{t("loginCinematic.hero.scrollHint")}</span>
            <ChevronDown className={layout.scrollArrowIcon} />
          </div>
        </div>
      </div>
    </>
  );
}
