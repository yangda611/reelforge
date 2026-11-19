// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  Bolt,
  ChevronRight,
  LogOut,
  KeyRound,
  Settings,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { NotificationDot } from "@/components/ui/notification-dot";
import { PhoneBindingDialog } from "@/components/account/phone-binding-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { CreditBalanceBadge } from "@/components/layout/credit-balance-badge";
import { BrandLockup } from "@/components/layout/brand-lockup";
import { SettingsDialog, type SettingsPage } from "@/components/settings/settings-dialog";
import {
  OPEN_SETTINGS_DIALOG_EVENT,
  openSettingsDialog,
  type OpenSettingsDialogDetail,
} from "@/components/settings/settings-dialog-events";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/auth-store";
import { authRequired, isCeRuntime, phoneOtpEntryVisible } from "@/lib/runtime-config";
import { resetUserSessionState } from "@/lib/reset-region-state";
import { useModelGatewayConfig } from "@/lib/queries/model-gateway";
import { useOrgBranding } from "@/lib/queries/org-branding";
import { useAccountSecurity } from "@/lib/queries/auth";
import {
  ProjectHeaderNavigation,
  ProjectSwitcher,
  ProjectXiajiMenu,
  ProjectXiahuaMenu,
} from "@/components/layout/project-header-navigation";
import entryStyles from "./header-entry.module.css";
import { APP_HEADER_HEIGHT } from "./layout-metrics";
const ACCOUNT_PANEL_TRANSITION_MS = 150;

export function Header({ ambientBackground = false }: { ambientBackground?: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const params = useParams({ strict: false }) as { project?: string };
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialPage, setSettingsInitialPage] = useState<SettingsPage>();
  const [phoneBindingOpen, setPhoneBindingOpen] = useState(false);
  const [accountPanelOpen, setAccountPanelOpen] = useState(false);
  const [accountPanelVisible, setAccountPanelVisible] = useState(false);
  const [settingsWarningBubbleDismissed, setSettingsWarningBubbleDismissed] = useState(false);
  const [accountPanelPosition, setAccountPanelPosition] = useState<{ top: number; right: number }>({
    top: 56,
    right: 16,
  });
  const accountHoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const accountCloseTimerRef = useRef<number | null>(null);
  const accountUnmountTimerRef = useRef<number | null>(null);
  const accountOpenFrameRef = useRef<number | null>(null);
  const accountAnchorRef = useRef<HTMLDivElement | null>(null);
  const accountTriggerRef = useRef<HTMLButtonElement | null>(null);
  const accountPanelRef = useRef<HTMLDivElement | null>(null);
  // 面板由点击/键盘打开时「钉住」：此时鼠标移开不再收走它。悬停打开的面板不钉。
  const accountPanelPinnedRef = useRef(false);
  const { username, displayName: storedDisplayName, logout } = useAuthStore();

  useEffect(() => {
    const handleOpenSettings = (event: Event) => {
      const page = (event as CustomEvent<OpenSettingsDialogDetail>).detail?.page;
      setSettingsInitialPage(page);
      setSettingsOpen(true);
    };
    window.addEventListener(OPEN_SETTINGS_DIALOG_EVENT, handleOpenSettings);
    return () => window.removeEventListener(OPEN_SETTINGS_DIALOG_EVENT, handleOpenSettings);
  }, []);
  const queryClient = useQueryClient();
  // 退出登录是 SPA 内部跳转（不刷新页面），必须一并清掉 React Query 缓存和
  // 用户级 zustand/localStorage 状态，否则换账号登录后 projectSummaries 等
  // 查询还在 staleTime 内，新账号会直接看到上一个账号的项目列表。
  const handleLogout = async () => {
    await logout();
    resetUserSessionState({ queryClient });
  };
  const avatarUrl = useAuthStore((s) => s.avatarUrl);
  const showLogout = authRequired();
  const ceRuntime = isCeRuntime();
  const orgBranding = useOrgBranding(!ceRuntime && Boolean(username));
  const brandName = orgBranding.data?.branding
    ? orgBranding.data.organization?.name ?? null
    : null;
  const homeLinkLabel = brandName
    ? `${t("app.logoHomeTooltip")} — ${brandName}`
    : t("app.logoHomeTooltip");
  const accountSecurity = useAccountSecurity(!ceRuntime && showLogout && Boolean(username));
  const passwordConfigured = accountSecurity.data?.password_configured ?? true;
  const canBindPhone = !ceRuntime && showLogout && phoneOtpEntryVisible()
    && accountSecurity.data?.phone === null && accountSecurity.data?.password_configured === true;
  const displayName = accountSecurity.data?.phone_masked ?? storedDisplayName ?? username ?? "User";
  const avatarInitial = displayName.slice(0, 1).toUpperCase();
  const modelGatewayConfig = useModelGatewayConfig(ceRuntime);
  const gatewayConfig = modelGatewayConfig.data?.data;
  // P1-2：黄标要说明缺了什么、点开去哪补。逐项列出未配置项，点击去第一项，
  // 悬停用 tooltip 说清每一项（媒体存储那项还点明会坏什么功能）。
  const missingSettingsItems = useMemo(() => {
    if (!ceRuntime || !gatewayConfig) return [];
    const items: { key: "gateway" | "mediaStorage"; page: SettingsPage }[] = [];
    if (gatewayConfig.effective.configured === false) {
      items.push({ key: "gateway", page: "models" });
    }
    if (gatewayConfig.mediaRelay?.configured === false) {
      items.push({ key: "mediaStorage", page: "storage" });
    }
    return items;
  }, [ceRuntime, gatewayConfig]);
  const hasSettingsWarning = missingSettingsItems.length > 0;
  const settingsWarningTooltip = missingSettingsItems
    .map((item) => t(`header.settingsWarningTooltip.${item.key}`))
    .join("；");
  const settingsWarningBubble = useFloatingBubblePosition(
    accountAnchorRef,
    hasSettingsWarning && !settingsOpen && !accountPanelOpen && !settingsWarningBubbleDismissed,
  );
  const project = params.project ?? null;

  useEffect(() => {
    return () => {
      if (accountHoverTimerRef.current) clearTimeout(accountHoverTimerRef.current);
      clearAccountCloseTimer();
      clearAccountUnmountTimer();
      clearAccountOpenFrame();
    };
  }, []);


  useEffect(() => {
    if (!hasSettingsWarning) {
      setSettingsWarningBubbleDismissed(false);
    }
  }, [hasSettingsWarning]);

  const clearAccountCloseTimer = () => {
    if (accountCloseTimerRef.current === null) return;
    window.clearTimeout(accountCloseTimerRef.current);
    accountCloseTimerRef.current = null;
  };

  const clearAccountUnmountTimer = () => {
    if (accountUnmountTimerRef.current === null) return;
    window.clearTimeout(accountUnmountTimerRef.current);
    accountUnmountTimerRef.current = null;
  };

  const clearAccountOpenFrame = () => {
    if (accountOpenFrameRef.current === null) return;
    window.cancelAnimationFrame(accountOpenFrameRef.current);
    accountOpenFrameRef.current = null;
  };

  const closeAccountPanelNow = () => {
    if (accountHoverTimerRef.current) clearTimeout(accountHoverTimerRef.current);
    accountPanelPinnedRef.current = false;
    clearAccountCloseTimer();
    clearAccountOpenFrame();
    clearAccountUnmountTimer();
    setAccountPanelVisible(false);
    setAccountPanelOpen(false);
  };

  const openAccountPanel = () => {
    if (accountHoverTimerRef.current) clearTimeout(accountHoverTimerRef.current);
    clearAccountCloseTimer();
    clearAccountUnmountTimer();
    clearAccountOpenFrame();
    const rect = accountAnchorRef.current?.getBoundingClientRect();
    if (rect) {
      setAccountPanelPosition({
        top: Math.round(rect.bottom + 8),
        right: Math.round(window.innerWidth - rect.right),
      });
    }
    setAccountPanelOpen(true);
    accountOpenFrameRef.current = window.requestAnimationFrame(() => {
      setAccountPanelVisible(true);
      accountOpenFrameRef.current = null;
    });
  };

  const scheduleOpenAccountPanel = () => {
    clearAccountCloseTimer();
    clearAccountUnmountTimer();
    if (accountPanelOpen) { openAccountPanel(); return; }
    if (accountHoverTimerRef.current) clearTimeout(accountHoverTimerRef.current);
    accountHoverTimerRef.current = setTimeout(openAccountPanel, 100);
  };

  const scheduleCloseAccountPanel = () => {
    if (accountHoverTimerRef.current) clearTimeout(accountHoverTimerRef.current);
    // 钉住的面板只由 Escape、面板外交互或选中某一项关闭 —— 鼠标掠过就收走的话,
    // 触屏和键盘用户点开后根本读不完里面的内容。
    if (accountPanelPinnedRef.current) return;
    clearAccountCloseTimer();
    accountCloseTimerRef.current = window.setTimeout(() => {
      setAccountPanelVisible(false);
      clearAccountUnmountTimer();
      accountUnmountTimerRef.current = window.setTimeout(() => {
        setAccountPanelOpen(false);
        accountUnmountTimerRef.current = null;
      }, ACCOUNT_PANEL_TRANSITION_MS);
      accountCloseTimerRef.current = null;
    }, 180);
  };

  const toggleAccountPanel = () => {
    if (accountPanelOpen && accountPanelPinnedRef.current) {
      closeAccountPanelNow();
      return;
    }
    accountPanelPinnedRef.current = true;
    openAccountPanel();
  };

  // 账号面板必须在指针、触屏和键盘下都能开关。
  // 面板 portal 到 body,不在触发器的 DOM 子树里,所以「点到外面」「焦点移到外面」
  // 只能在 document 上判定。
  useEffect(() => {
    if (!accountPanelOpen) return;
    const isInsideAccountUi = (target: EventTarget | null) =>
      target instanceof Node
      && (Boolean(accountAnchorRef.current?.contains(target))
        || Boolean(accountPanelRef.current?.contains(target)));
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      closeAccountPanelNow();
      accountTriggerRef.current?.focus();
    };
    const handlePointerDown = (event: PointerEvent) => {
      if (isInsideAccountUi(event.target)) return;
      closeAccountPanelNow();
    };
    const handleFocusIn = (event: FocusEvent) => {
      // Hover panels do not own focus; another popup may restore it on exit.
      if (!accountPanelPinnedRef.current || isInsideAccountUi(event.target)) return;
      closeAccountPanelNow();
    };
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("focusin", handleFocusIn);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("focusin", handleFocusIn);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountPanelOpen]);

  // 面板挂在 body 末尾,Tab 不会从触发器走进去。点击/键盘打开时把焦点送进第一项,
  // 否则键盘用户能打开面板却够不到里面的选项。悬停打开的面板不抢焦点。
  useEffect(() => {
    if (!accountPanelOpen || !accountPanelPinnedRef.current) return;
    accountPanelRef.current
      ?.querySelector<HTMLElement>('button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')
      ?.focus();
  }, [accountPanelOpen]);

  const handlePasswordChanged = () => {
    resetUserSessionState({ queryClient });
    void navigate({ to: "/login", replace: true });
  };

  return (
    <div className="relative shrink-0 text-sidebar-foreground">
      <header style={{ height: APP_HEADER_HEIGHT }} className={`relative z-20 flex items-center justify-between gap-3 px-4 ${ambientBackground ? "bg-transparent" : "bg-background/58 backdrop-blur-xl"}`}>
        <div className="flex min-w-0 flex-1 items-center">
          <TooltipProvider delay={80}>
            <Tooltip>
              <TooltipTrigger
                render={
                  <Link
                    to="/"
                    aria-label={homeLinkLabel}
                    className="flex min-w-0 shrink-0 items-center"
                  />
                }
              >
                <BrandLockup value={orgBranding.data} />
              </TooltipTrigger>
              <TooltipContent
                side="bottom"
                sideOffset={10}
                showArrow={false}
                className="border border-white/10 bg-background/95 text-foreground shadow-none"
              >
                {t("app.logoHomeTooltip")}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <div className="ml-[22px] flex min-w-0 items-center gap-6">
            {project ? <ProjectSwitcher current={project} /> : null}
          </div>
        </div>

        {project ? <ProjectHeaderNavigation project={project} /> : null}

        {/* Actions */}
        <div className="flex min-w-0 flex-1 shrink-0 items-center justify-end gap-1">
          {/* P2-2：顶栏直达设置。 */}
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className={`${entryStyles.trigger} relative size-[28px]`}
            aria-label={t("header.settings")}
            title={t("header.settings")}
            onClick={() => {
              setSettingsInitialPage(undefined);
              setSettingsOpen(true);
            }}
          >
            <Settings className="size-4" aria-hidden />
          </Button>
          <CreditBalanceBadge />
          <div
            id="superchat-header-controls"
            className="flex min-w-0 shrink items-center gap-2 empty:hidden"
          />
          <div
            ref={accountAnchorRef}
            className="relative ml-1 flex items-center"
            onMouseEnter={scheduleOpenAccountPanel}
            onMouseLeave={scheduleCloseAccountPanel}
          >
            <Button
              ref={accountTriggerRef}
              type="button"
              variant="ghost"
              size="icon-sm"
              className={`${entryStyles.trigger} relative size-[28px] rounded-full p-0`}
              aria-label={t("header.account.open")}
              aria-haspopup="true"
              aria-expanded={accountPanelOpen}
              aria-controls={accountPanelOpen ? "header-account-panel" : undefined}
              onClick={toggleAccountPanel}
            >
              <span data-account-avatar className="flex size-[26px] items-center justify-center overflow-hidden rounded-full border border-white/[0.10] bg-white/[0.07] text-[11px] font-normal text-white/72">
                {avatarUrl ? (
                  <img src={avatarUrl} alt="" className="size-full object-cover" />
                ) : (
                  avatarInitial
                )}
              </span>
              {hasSettingsWarning ? (
                <NotificationDot className="absolute right-0 top-0" />
              ) : null}
            </Button>
          </div>
        </div>
      </header>
      {project ? <ProjectXiajiMenu project={project} /> : null}
      {project ? <ProjectXiahuaMenu /> : null}
      {accountPanelOpen
        ? createPortal(
            <AccountPanel
              avatarInitial={avatarInitial}
              avatarUrl={avatarUrl}
              displayName={displayName}
              hasSettingsWarning={hasSettingsWarning}
              onSettings={() => {
                closeAccountPanelNow();
                setSettingsInitialPage(undefined);
                setSettingsOpen(true);
              }}
              onBindPhone={canBindPhone ? () => {
                closeAccountPanelNow();
                setPhoneBindingOpen(true);
              } : undefined}
              onClose={scheduleCloseAccountPanel}
              onEnter={openAccountPanel}
              onLogout={showLogout ? () => void handleLogout() : undefined}
              panelRef={accountPanelRef}
              position={accountPanelPosition}
              visible={accountPanelVisible}
              t={t}
            />,
            document.body,
          )
        : null}
      {phoneBindingOpen && canBindPhone ? <PhoneBindingDialog
        key={username}
        onClose={() => setPhoneBindingOpen(false)}
        onBound={() => { void accountSecurity.refetch(); }}
      /> : null}
      <SettingsDialog
        open={settingsOpen}
        initialPage={settingsInitialPage}
        onOpenChange={(open) => {
          setSettingsOpen(open);
          if (!open) setSettingsInitialPage(undefined);
        }}
        account={{
          avatarInitial,
          displayName,
          passwordConfigured,
          onPasswordChanged: showLogout ? handlePasswordChanged : undefined,
        }}
      />
      {settingsWarningBubble
        ? createPortal(
            <div
              className="fixed z-[9999] w-[112px] cursor-pointer rounded-md border border-amber-400/45 bg-amber-400 py-1 pl-2 pr-6 text-[11px] font-medium leading-none text-black shadow-[0_8px_22px_rgba(0,0,0,0.36),0_0_12px_rgba(251,191,36,0.28)]"
              style={{ left: settingsWarningBubble.left, top: settingsWarningBubble.top }}
              role="button"
              tabIndex={0}
              title={settingsWarningTooltip}
              aria-label={settingsWarningTooltip}
              onClick={() => {
                const first = missingSettingsItems[0];
                if (first) openSettingsDialog(first.page);
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                const first = missingSettingsItems[0];
                if (first) openSettingsDialog(first.page);
              }}
            >
              <span
                className="absolute -top-[4px] size-2 rotate-45 border-l border-t border-amber-400/45 bg-amber-400"
                style={{ left: settingsWarningBubble.arrowLeft }}
                aria-hidden="true"
              />
              <span className="block truncate">{t("header.settingsWarningBubble")}</span>
              <button
                type="button"
                className="absolute right-1 top-1/2 flex size-4 -translate-y-1/2 items-center justify-center rounded-full text-black/70 transition-colors hover:bg-black/10 hover:text-black"
                aria-label={t("header.dismissSettingsWarningBubble")}
                onClick={(event) => {
                  event.stopPropagation();
                  setSettingsWarningBubbleDismissed(true);
                }}
              >
                <X className="size-3" strokeWidth={3} />
              </button>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

function useFloatingBubblePosition(
  anchorRef: RefObject<HTMLElement | null>,
  enabled: boolean,
): { left: number; top: number; arrowLeft: number } | null {
  const [position, setPosition] = useState<{ left: number; top: number; arrowLeft: number } | null>(
    null,
  );

  useEffect(() => {
    if (!enabled) {
      setPosition(null);
      return;
    }

    const update = () => {
      const rect = anchorRef.current?.getBoundingClientRect();
      if (!rect) {
        setPosition(null);
        return;
      }
      const bubbleWidth = 112;
      const viewportPadding = 8;
      const idealLeft = rect.left + rect.width / 2 - bubbleWidth / 2;
      const left = Math.min(
        Math.max(viewportPadding, idealLeft),
        window.innerWidth - bubbleWidth - viewportPadding,
      );
      setPosition({
        left,
        top: rect.bottom + 7,
        arrowLeft: rect.left + rect.width / 2 - left - 4,
      });
    };

    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [anchorRef, enabled]);

  return position;
}

function AccountPanel({
  avatarInitial,
  avatarUrl,
  displayName,
  hasSettingsWarning,
  onSettings,
  onBindPhone,
  onClose,
  onEnter,
  onLogout,
  panelRef,
  position,
  visible,
  t,
}: {
  avatarInitial: string;
  avatarUrl: string | null;
  displayName: string;
  hasSettingsWarning: boolean;
  onSettings: () => void;
  onBindPhone?: () => void;
  onClose: () => void;
  onEnter: () => void;
  onLogout?: () => void;
  panelRef: RefObject<HTMLDivElement | null>;
  position: { top: number; right: number };
  visible: boolean;
  t: (key: string) => string;
}) {
  return (
    <div
      ref={panelRef}
      id="header-account-panel"
      aria-label={t("header.account.open")}
      className={`fixed z-[80] w-[216px] transition-opacity duration-150 ease-[var(--ease-out-quint)] motion-reduce:transition-none ${
        visible ? "opacity-100" : "opacity-0"
      }`}
      style={{ top: position.top, right: position.right }}
      onMouseEnter={onEnter}
      onMouseLeave={onClose}
    >
      <HeaderMenuPanel>
        <div className="mb-2.5 flex h-[50px] items-center gap-2.5 rounded-[10px] bg-white/[0.07] px-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center overflow-hidden rounded-full border border-white/[0.10] bg-white/[0.07] text-xs font-normal text-white/72">
            {avatarUrl ? (
              <img src={avatarUrl} alt="" className="size-full object-cover" />
            ) : (
              avatarInitial
            )}
          </span>
          <span className="min-w-0 truncate text-[15px] font-medium text-white">
            {displayName}
          </span>
        </div>
        <div className="space-y-0.5">
          <HeaderMenuRow
            icon={hasSettingsWarning ? <AlertTriangle className="size-3.5 text-amber-400" /> : <Bolt className="size-3.5" />}
            label={t(hasSettingsWarning ? "header.settingsWithWarning" : "header.settings")}
            onClick={onSettings}
          />
          {onBindPhone ? <HeaderMenuRow
            icon={<KeyRound className="size-3.5" />}
            label={t("header.account.phoneBinding.title")}
            onClick={onBindPhone}
          /> : null}
          {onLogout ? (
            <HeaderMenuRow
              icon={<LogOut className="size-3.5" />}
              label={t("auth.logout")}
              onClick={onLogout}
            />
          ) : null}
        </div>
      </HeaderMenuPanel>
    </div>
  );
}

function HeaderMenuPanel({ children }: {
  children: ReactNode;
}) {
  const className = `${entryStyles.panel} ${entryStyles.menuPanel}`;
  return <div className={className}>{children}</div>;
}

function HeaderMenuRow({
  id,
  icon,
  label,
  onClick,
}: {
  id?: string;
  icon: ReactNode;
  label: string;
  onClick?: () => void;
}) {
  const content = (
    <>
      <span className={`${entryStyles.menuIcon} ml-1 flex size-3.5 shrink-0 items-center justify-center`} aria-hidden="true">
        {icon}
      </span>
      <span className={`${entryStyles.menuLabel} flex min-w-0 flex-1 items-center gap-1.5`}>
        <span className="truncate">{label}</span>
      </span>
      <ChevronRight
        className={`${entryStyles.menuChevron} mr-1 size-3.5 shrink-0`}
      />
    </>
  );
  const className = entryStyles.menuRow;
  return (
    <button id={id} type="button" className={className} onClick={onClick}>
      {content}
    </button>
  );
}
