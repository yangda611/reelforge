// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

import { useGenerateSeedance2Prompt } from "@/lib/queries/video";
import { BillingRuleNotConfiguredError } from "@/lib/api-errors";
import { useAppStore } from "@/stores/app-store";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  useAppStore.setState({ language: "zh" });
});
afterAll(() => server.close());

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("Seedance2 prompt generation query", () => {
  it("posts current prompt reference and guidance to the per-beat endpoint", async () => {
    let requestedPath = "";
    let body: unknown = null;
    server.use(
      http.post(
        "http://localhost:3000/api/v1/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
        async ({ request }) => {
          requestedPath = new URL(request.url).pathname;
          body = await request.json();
          return HttpResponse.json({
            ok: true,
            data: {
              final_prompt: "optimized seedance2 prompt",
              seedance2_config_json:
                '{"final_prompt":"optimized seedance2 prompt"}',
              beat: {
                beat_number: 2,
                seedance2_config_json:
                  '{"final_prompt":"optimized seedance2 prompt"}',
              },
            },
          });
        },
      ),
    );

    const { result } = renderHook(() => useGenerateSeedance2Prompt("demo", 1), {
      wrapper,
    });
    result.current.mutate({
      beatNum: 2,
      manualPromptReference: "current prompt",
      promptGuidance: "more camera motion",
      promptGuidanceTemplateKeys: ["camera"],
    });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(requestedPath).toBe(
      "/api/v1/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
    );
    expect(body).toEqual({
      manual_prompt_reference: "current prompt",
      prompt_guidance: "more camera motion",
      prompt_guidance_template_keys: ["camera"],
    });
    expect(result.current.data?.ok).toBe(true);
  });

  it("does not use the current app language to choose the Seedance2 output language", async () => {
    useAppStore.setState({ language: "en" });
    let body: unknown = null;
    server.use(
      http.post(
        "http://localhost:3000/api/v1/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
        async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({
            ok: true,
            data: {
              final_prompt: "optimized seedance2 prompt",
              seedance2_config_json:
                '{"final_prompt":"optimized seedance2 prompt"}',
              beat: {
                beat_number: 2,
                seedance2_config_json:
                  '{"final_prompt":"optimized seedance2 prompt"}',
              },
            },
          });
        },
      ),
    );

    const { result } = renderHook(() => useGenerateSeedance2Prompt("demo", 1), {
      wrapper,
    });
    result.current.mutate({ beatNum: 2 });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(body).toEqual({
      manual_prompt_reference: "",
      prompt_guidance: "",
      prompt_guidance_template_keys: [],
    });
  });

  it("forwards promptStyle and videoBackend when provided", async () => {
    let body: unknown = null;
    server.use(
      http.post(
        "http://localhost:3000/api/v1/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
        async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({
            ok: true,
            data: {
              final_prompt: "optimized prompt",
              seedance2_config_json: '{"final_prompt":"optimized prompt"}',
              beat: {
                beat_number: 2,
                seedance2_config_json: '{"final_prompt":"optimized prompt"}',
              },
            },
          });
        },
      ),
    );

    const { result } = renderHook(() => useGenerateSeedance2Prompt("demo", 1), {
      wrapper,
    });
    result.current.mutate({
      beatNum: 2,
      promptStyle: "minimax_h3",
      videoBackend: "newapi_Seedance2.0",
    });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(body).toEqual({
      manual_prompt_reference: "",
      prompt_guidance: "",
      prompt_guidance_template_keys: [],
      prompt_style: "minimax_h3",
      video_backend: "newapi_Seedance2.0",
    });
  });

  it("parses feature billing errors from the Seedance2 prompt endpoint", async () => {
    server.use(
      http.post(
        "http://localhost:3000/api/v1/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
        () =>
          HttpResponse.json(
            {
              ok: false,
              error: "计费规则未配置，请联系管理员设置积分规则",
              data: {
                error_code: "BILLING_RULE_NOT_CONFIGURED",
                billing_kind: "feature",
                billing_key: "mainline.seedance2_prompt",
              },
            },
            { status: 409 },
          ),
      ),
    );

    const { result } = renderHook(() => useGenerateSeedance2Prompt("demo", 1), {
      wrapper,
    });
    result.current.mutate({
      beatNum: 2,
      manualPromptReference: "current prompt",
      promptGuidance: "more camera motion",
    });

    await waitFor(() => expect(result.current.error).toBeDefined());
    expect(result.current.error).toBeInstanceOf(BillingRuleNotConfiguredError);
  });

  it("passes an extended timeout for long single-beat prompt generation", async () => {
    server.use(
      http.post(
        "http://localhost:3000/api/v1/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
        () =>
          HttpResponse.json({
            ok: true,
            data: {
              beat: { beat_number: 2, seedance2_config_json: JSON.stringify({ final_prompt: "ok" }) },
              seedance2_config: { final_prompt: "ok" },
            },
          }),
      ),
    );

    const { api } = await import("@/lib/api");
    const postSpy = vi.spyOn(api, "post");

    const { result } = renderHook(
      () => useGenerateSeedance2Prompt("demo", 1),
      { wrapper },
    );

    await result.current.mutateAsync({ beatNum: 2, promptStyle: "s9_segment" });

    expect(postSpy).toHaveBeenCalledWith(
      "api/v1/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
      expect.objectContaining({
        timeout: 600_000,
      }),
    );
    postSpy.mockRestore();
  });
});
