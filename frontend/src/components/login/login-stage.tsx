// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useTranslation } from "react-i18next";
import { CommunityShowcase } from "./community-showcase";
import LightRays from "./light-rays";
import SplitText from "@/components/react-bits/split-text";
import styles from "./login.module.css";

export function Brand({ className }: { className?: string }) {
  return (
    <div className={className ?? styles.brand} aria-label="ReelForge">
      <span className={styles.brandLogo} aria-hidden="true">
        ReelForge
      </span>
    </div>
  );
}

/**
 * Stage contents — render inside an element already styled with `styles.stage`.
 */
export function LoginStageContent({
  onStart,
}: {
  onStart: () => void;
}) {
  const { t } = useTranslation();

  return (
    <>
      <div className={styles.stageLightRays} aria-hidden="true">
        <LightRays
          raysOrigin="top-center"
          raysColor="#ffffff"
          raysSpeed={1}
          lightSpread={0.5}
          rayLength={3}
          pulsating={false}
          fadeDistance={1}
          saturation={1}
          followMouse={false}
          mouseInfluence={0.1}
          noiseAmount={0}
          distortion={0}
        />
      </div>

      <div className={styles.stageInner}>
        <div className={styles.stageTopBar}>
          <Brand />
        </div>

        <div className={styles.hero}>
          <SplitText
            tag="h1"
            text={t("auth.stage.headlines.createUniverse")}
            className={styles.heroTitle}
            delay={70}
            duration={0.8}
            ease="power3.out"
            splitType="chars"
            from={{ opacity: 0, y: 36 }}
            to={{ opacity: 1, y: 0 }}
            threshold={0.1}
            rootMargin="-100px"
            textAlign="center"
          />
          <p className={styles.heroSubtitle}>
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
          <div className={styles.heroActions}>
            <button
              type="button"
              className={styles.heroPrimary}
              onClick={onStart}
            >
              {t("auth.stage.start")}
            </button>
          </div>
        </div>

        <CommunityShowcase />
      </div>
    </>
  );
}
