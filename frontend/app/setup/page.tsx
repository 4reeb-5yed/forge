"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  ConfigResponse,
  KeyTestResult,
  getConfig,
  updateConfig,
  testConfigKey,
  getConfigModels,
  getHealth,
} from "@/lib/api";

type Step = 1 | 2 | 3 | 4;

export default function SetupPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>(1);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [openrouterKey, setOpenrouterKey] = useState("");
  const [githubToken, setGithubToken] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [sandboxMode, setSandboxMode] = useState<"always" | "auto" | "never">("auto");
  const [apiToken, setApiToken] = useState("");

  // Load API token from localStorage on mount
  useEffect(() => {
    const stored = localStorage.getItem("forge_api_token");
    if (stored) setApiToken(stored);
  }, []);

  // Test results
  const [openrouterTest, setOpenrouterTest] = useState<KeyTestResult | null>(null);
  const [githubTest, setGithubTest] = useState<KeyTestResult | null>(null);
  const [testingOpenrouter, setTestingOpenrouter] = useState(false);
  const [testingGithub, setTestingGithub] = useState(false);

  // Models
  const [models, setModels] = useState<Array<{ id: string; name: string }>>([]);
  const [loadingModels, setLoadingModels] = useState(false);

  // Docker status
  const [dockerAvailable, setDockerAvailable] = useState<boolean | null>(null);

  // Pre-populate from existing config
  useEffect(() => {
    async function loadExisting() {
      try {
        const config = await getConfig();
        if (config.openrouter_api_key) setOpenrouterKey(config.openrouter_api_key);
        if (config.github_token) setGithubToken(config.github_token);
        if (config.selected_model) setSelectedModel(config.selected_model);
        if (config.sandbox_mode) setSandboxMode(config.sandbox_mode as "always" | "auto" | "never");
      } catch {
        // First run, no config yet
      }
    }
    loadExisting();
  }, []);

  // Load models when reaching step 2
  useEffect(() => {
    if (step === 2) {
      loadModels();
    }
  }, [step]);

  // Check Docker when reaching step 3
  useEffect(() => {
    if (step === 3) {
      checkDocker();
    }
  }, [step]);

  async function loadModels() {
    setLoadingModels(true);
    try {
      const result = await getConfigModels();
      setModels(result.models || []);
    } catch {
      setModels([]);
    } finally {
      setLoadingModels(false);
    }
  }

  async function checkDocker() {
    try {
      const health = await getHealth();
      const docker = health.components?.docker;
      setDockerAvailable(docker?.status === "healthy");
    } catch {
      setDockerAvailable(null);
    }
  }

  async function handleTestOpenrouter() {
    setTestingOpenrouter(true);
    setOpenrouterTest(null);
    try {
      const result = await testConfigKey("openrouter", openrouterKey);
      setOpenrouterTest(result);
    } catch (err) {
      setOpenrouterTest({
        success: false,
        latency_ms: 0,
        error: err instanceof Error ? err.message : "Test failed",
      });
    } finally {
      setTestingOpenrouter(false);
    }
  }

  async function handleTestGithub() {
    setTestingGithub(true);
    setGithubTest(null);
    try {
      const result = await testConfigKey("github", githubToken);
      setGithubTest(result);
    } catch (err) {
      setGithubTest({
        success: false,
        latency_ms: 0,
        error: err instanceof Error ? err.message : "Test failed",
      });
    } finally {
      setTestingGithub(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      // Save API token to localStorage
      if (apiToken) {
        localStorage.setItem("forge_api_token", apiToken);
      }
      await updateConfig({
        openrouter_api_key: openrouterKey,
        github_token: githubToken,
        selected_model: selectedModel,
        sandbox_mode: sandboxMode,
      });
      router.push("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save configuration");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-forge-bg flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-lg">
        {/* Header */}
        <div className="text-center mb-8">
          <div className="flex items-center justify-center gap-2 mb-3">
            <svg className="w-8 h-8 text-forge-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            <h1 className="text-2xl font-bold text-forge-text">Forge Setup</h1>
          </div>
          <p className="text-sm text-forge-muted">Configure Forge to get started</p>
        </div>

        {/* Step indicator */}
        <div className="flex items-center justify-center gap-2 mb-8">
          {[1, 2, 3, 4].map((s) => (
            <div key={s} className="flex items-center gap-2">
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-medium ${
                  s === step
                    ? "bg-forge-accent text-white"
                    : s < step
                    ? "bg-green-500 text-white"
                    : "bg-forge-card text-forge-muted border border-forge-border"
                }`}
              >
                {s < step ? "✓" : s}
              </div>
              {s < 4 && (
                <div className={`w-8 h-0.5 ${s < step ? "bg-green-500" : "bg-forge-border"}`} />
              )}
            </div>
          ))}
        </div>

        {/* Step content */}
        <div className="bg-forge-card border border-forge-border rounded-lg p-6">
          {step === 1 && (
            <StepApiKeys
              openrouterKey={openrouterKey}
              setOpenrouterKey={setOpenrouterKey}
              githubToken={githubToken}
              setGithubToken={setGithubToken}
              openrouterTest={openrouterTest}
              githubTest={githubTest}
              testingOpenrouter={testingOpenrouter}
              testingGithub={testingGithub}
              onTestOpenrouter={handleTestOpenrouter}
              onTestGithub={handleTestGithub}
            />
          )}

          {step === 2 && (
            <StepModelSelection
              models={models}
              loadingModels={loadingModels}
              selectedModel={selectedModel}
              setSelectedModel={setSelectedModel}
            />
          )}

          {step === 3 && (
            <StepSandboxMode
              sandboxMode={sandboxMode}
              setSandboxMode={setSandboxMode}
              dockerAvailable={dockerAvailable}
            />
          )}

          {step === 4 && (
            <StepApiToken
              apiToken={apiToken}
              setApiToken={setApiToken}
            />
          )}

          {/* Error display */}
          {error && (
            <div className="mt-4 p-3 rounded bg-red-500/10 border border-red-500/30 text-sm text-red-400">
              {error}
            </div>
          )}

          {/* Navigation */}
          <div className="flex items-center justify-between mt-6 pt-4 border-t border-forge-border">
            {step > 1 ? (
              <button
                onClick={() => setStep((step - 1) as Step)}
                className="text-sm px-4 py-2 rounded border border-forge-border text-forge-muted hover:text-forge-text transition-colors"
              >
                Back
              </button>
            ) : (
              <div />
            )}

            {step < 4 ? (
              <button
                onClick={() => setStep((step + 1) as Step)}
                disabled={step === 1 && !openrouterKey}
                className="text-sm px-4 py-2 rounded bg-forge-accent text-white hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Continue
              </button>
            ) : (
              <button
                onClick={handleSave}
                disabled={saving || !openrouterKey || !selectedModel}
                className="text-sm px-4 py-2 rounded bg-green-600 text-white hover:bg-green-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {saving ? "Saving..." : "Save & Continue"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1: API Keys
// ---------------------------------------------------------------------------

interface StepApiKeysProps {
  openrouterKey: string;
  setOpenrouterKey: (v: string) => void;
  githubToken: string;
  setGithubToken: (v: string) => void;
  openrouterTest: KeyTestResult | null;
  githubTest: KeyTestResult | null;
  testingOpenrouter: boolean;
  testingGithub: boolean;
  onTestOpenrouter: () => void;
  onTestGithub: () => void;
}

function StepApiKeys({
  openrouterKey,
  setOpenrouterKey,
  githubToken,
  setGithubToken,
  openrouterTest,
  githubTest,
  testingOpenrouter,
  testingGithub,
  onTestOpenrouter,
  onTestGithub,
}: StepApiKeysProps) {
  return (
    <div>
      <h2 className="text-lg font-semibold text-forge-text mb-1">API Keys</h2>
      <p className="text-sm text-forge-muted mb-6">
        Enter your API keys. OpenRouter is required for LLM access.
      </p>

      {/* OpenRouter */}
      <div className="mb-5">
        <label className="block text-sm text-forge-text mb-1.5">
          OpenRouter API Key <span className="text-forge-error">*</span>
        </label>
        <div className="flex gap-2">
          <input
            type="password"
            value={openrouterKey}
            onChange={(e) => setOpenrouterKey(e.target.value)}
            placeholder="sk-or-v1-..."
            className="flex-1 px-3 py-2 rounded bg-forge-bg border border-forge-border text-sm text-forge-text placeholder:text-forge-muted/50 focus:outline-none focus:border-forge-accent"
          />
          <button
            onClick={onTestOpenrouter}
            disabled={!openrouterKey || testingOpenrouter}
            className="text-xs px-3 py-2 rounded border border-forge-border text-forge-muted hover:text-forge-text disabled:opacity-50 transition-colors"
          >
            {testingOpenrouter ? "Testing..." : "Test"}
          </button>
        </div>
        {openrouterTest && <TestResultDisplay result={openrouterTest} />}
      </div>

      {/* GitHub */}
      <div>
        <label className="block text-sm text-forge-text mb-1.5">
          GitHub Token <span className="text-forge-muted text-xs">(optional)</span>
        </label>
        <div className="flex gap-2">
          <input
            type="password"
            value={githubToken}
            onChange={(e) => setGithubToken(e.target.value)}
            placeholder="ghp_..."
            className="flex-1 px-3 py-2 rounded bg-forge-bg border border-forge-border text-sm text-forge-text placeholder:text-forge-muted/50 focus:outline-none focus:border-forge-accent"
          />
          <button
            onClick={onTestGithub}
            disabled={!githubToken || testingGithub}
            className="text-xs px-3 py-2 rounded border border-forge-border text-forge-muted hover:text-forge-text disabled:opacity-50 transition-colors"
          >
            {testingGithub ? "Testing..." : "Test"}
          </button>
        </div>
        {githubTest && <TestResultDisplay result={githubTest} />}
      </div>
    </div>
  );
}

function TestResultDisplay({ result }: { result: KeyTestResult }) {
  if (result.success) {
    return (
      <p className="mt-1.5 text-xs text-green-400">
        ✓ Valid ({result.latency_ms}ms)
      </p>
    );
  }
  return (
    <p className="mt-1.5 text-xs text-red-400">
      ✗ {result.error || "Test failed"}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Step 2: Model Selection
// ---------------------------------------------------------------------------

interface StepModelSelectionProps {
  models: Array<{ id: string; name: string }>;
  loadingModels: boolean;
  selectedModel: string;
  setSelectedModel: (v: string) => void;
}

function StepModelSelection({
  models,
  loadingModels,
  selectedModel,
  setSelectedModel,
}: StepModelSelectionProps) {
  return (
    <div>
      <h2 className="text-lg font-semibold text-forge-text mb-1">Model Selection</h2>
      <p className="text-sm text-forge-muted mb-6">
        Choose which LLM model Forge should use for code generation.
      </p>

      {loadingModels ? (
        <div className="text-sm text-forge-muted py-4 text-center">Loading models...</div>
      ) : models.length === 0 ? (
        <div className="text-sm text-forge-muted py-4 text-center">
          No models available. Make sure your OpenRouter key is valid.
        </div>
      ) : (
        <div>
          <label className="block text-sm text-forge-text mb-1.5">Model</label>
          <select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            className="w-full px-3 py-2 rounded bg-forge-bg border border-forge-border text-sm text-forge-text focus:outline-none focus:border-forge-accent"
          >
            <option value="">Select a model...</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} ({m.id})
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3: Sandbox Mode
// ---------------------------------------------------------------------------

interface StepSandboxModeProps {
  sandboxMode: "always" | "auto" | "never";
  setSandboxMode: (v: "always" | "auto" | "never") => void;
  dockerAvailable: boolean | null;
}

function StepSandboxMode({
  sandboxMode,
  setSandboxMode,
  dockerAvailable,
}: StepSandboxModeProps) {
  const options: Array<{ value: "always" | "auto" | "never"; label: string; description: string }> = [
    { value: "always", label: "Always", description: "All code runs in Docker sandbox" },
    { value: "auto", label: "Auto", description: "Use sandbox when available, skip when not" },
    { value: "never", label: "Never", description: "Run code directly on host (fastest)" },
  ];

  return (
    <div>
      <h2 className="text-lg font-semibold text-forge-text mb-1">Sandbox Mode</h2>
      <p className="text-sm text-forge-muted mb-6">
        Choose how Forge executes generated code.
      </p>

      {/* Docker status */}
      <div className="mb-5 p-3 rounded bg-forge-bg border border-forge-border">
        <div className="flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full ${
              dockerAvailable === true
                ? "bg-green-500"
                : dockerAvailable === false
                ? "bg-red-500"
                : "bg-forge-muted"
            }`}
          />
          <span className="text-xs text-forge-muted">
            Docker:{" "}
            {dockerAvailable === true
              ? "Available"
              : dockerAvailable === false
              ? "Not available"
              : "Checking..."}
          </span>
        </div>
      </div>

      {/* Radio options */}
      <div className="space-y-3">
        {options.map((opt) => (
          <label
            key={opt.value}
            className={`flex items-start gap-3 p-3 rounded border cursor-pointer transition-colors ${
              sandboxMode === opt.value
                ? "border-forge-accent bg-forge-accent/5"
                : "border-forge-border hover:border-forge-muted"
            }`}
          >
            <input
              type="radio"
              name="sandboxMode"
              value={opt.value}
              checked={sandboxMode === opt.value}
              onChange={() => setSandboxMode(opt.value)}
              className="mt-0.5 accent-forge-accent"
            />
            <div>
              <div className="text-sm text-forge-text font-medium">{opt.label}</div>
              <div className="text-xs text-forge-muted">{opt.description}</div>
            </div>
          </label>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 4: API Token
// ---------------------------------------------------------------------------

interface StepApiTokenProps {
  apiToken: string;
  setApiToken: (v: string) => void;
}

function StepApiToken({
  apiToken,
  setApiToken,
}: StepApiTokenProps) {
  return (
    <div>
      <h2 className="text-lg font-semibold text-forge-text mb-1">API Token (Optional)</h2>
      <p className="text-sm text-forge-muted mb-6">
        Enter the API token for authentication. If authentication is disabled on the server,
        you can leave this empty. This token is stored locally in your browser.
      </p>

      <div>
        <label className="block text-sm text-forge-text mb-1.5">
          API Token
        </label>
        <input
          type="password"
          value={apiToken}
          onChange={(e) => setApiToken(e.target.value)}
          placeholder="Enter your API token"
          className="w-full px-3 py-2 rounded bg-forge-bg border border-forge-border text-sm text-forge-text placeholder:text-forge-muted/50 focus:outline-none focus:border-forge-accent"
        />
        <p className="mt-2 text-xs text-forge-muted">
          This token will be sent as a Bearer token with API requests.
        </p>
      </div>
    </div>
  );
}
