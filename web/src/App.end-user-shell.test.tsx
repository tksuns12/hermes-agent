import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "./App";

interface TestPlugin {
  manifest: {
    icon: string;
    label: string;
    name: string;
    tab: {
      path: string;
      position?: string;
    };
  };
  component: () => ReactNode;
}

const pluginHarness = vi.hoisted(() => ({
  plugins: [] as TestPlugin[],
}));

vi.mock("@nous-research/ui", () => {
  const Div = ({ children }: { children?: unknown }) => (
    <div>{children as ReactNode}</div>
  );
  const Span = ({ children }: { children?: unknown }) => (
    <span>{children as ReactNode}</span>
  );

  return {
    Cell: Div,
    Grid: Div,
    SelectionSwitcher: () => <div data-testid="selection-switcher" />,
    Typography: Span,
  };
});

vi.mock("@/plugins", () => ({
  usePlugins: () => ({
    plugins: pluginHarness.plugins,
    manifests: pluginHarness.plugins.map((plugin) => plugin.manifest),
    loading: false,
  }),
}));

vi.mock("@/pages/WorkbenchPage", () => ({
  default: () => <div data-testid="workbench-page">Mock workbench content</div>,
}));

vi.mock("@/components/Backdrop", () => ({
  Backdrop: () => <div data-testid="mock-backdrop" />,
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

afterEach(() => {
  pluginHarness.plugins = [];
});

describe("App shell routing", () => {
  it("renders the end-user shell on / without operator chrome", () => {
    renderAt("/");

    expect(screen.getByTestId("end-user-shell")).toBeTruthy();
    expect(screen.getByTestId("end-user-home")).toBeTruthy();
    expect(screen.queryByTestId("operator-shell")).toBeNull();
    expect(screen.queryByRole("link", { name: /status/i })).toBeNull();
    expect(screen.queryByText("Nous Research")).toBeNull();
  });

  it("keeps /workbench on the operator shell", () => {
    renderAt("/workbench");

    expect(screen.getByTestId("operator-shell")).toBeTruthy();
    expect(screen.getByTestId("workbench-page")).toBeTruthy();
    expect(screen.getByRole("link", { name: /status/i })).toBeTruthy();
    expect(screen.getByText("Nous Research")).toBeTruthy();
    expect(screen.queryByTestId("end-user-shell")).toBeNull();
  });

  it("redirects unknown paths back to the end-user shell", () => {
    renderAt("/not-a-route");

    expect(screen.getByTestId("end-user-shell")).toBeTruthy();
    expect(screen.getByText(/work with your files in a live ai run\./i)).toBeTruthy();
    expect(screen.queryByTestId("operator-shell")).toBeNull();
  });

  it("keeps root in the end-user shell even when plugins are registered", () => {
    pluginHarness.plugins = [
      {
        manifest: {
          name: "plugin-home-check",
          label: "Plugin Home Check",
          icon: "Puzzle",
          tab: { path: "/plugin-home-check" },
        },
        component: () => <div>Plugin home check content</div>,
      },
    ];

    renderAt("/");

    expect(screen.getByTestId("end-user-shell")).toBeTruthy();
    expect(screen.queryByTestId("operator-shell")).toBeNull();
  });

  it("renders plugin routes inside the operator shell", () => {
    pluginHarness.plugins = [
      {
        manifest: {
          name: "plugin-insights",
          label: "Plugin Insights",
          icon: "Code",
          tab: { path: "/plugin-insights" },
        },
        component: () => <div data-testid="plugin-page">Plugin route content</div>,
      },
    ];

    renderAt("/plugin-insights");

    expect(screen.getByTestId("operator-shell")).toBeTruthy();
    expect(screen.getByTestId("plugin-page")).toBeTruthy();
    expect(screen.getByRole("link", { name: /plugin insights/i })).toBeTruthy();
    expect(screen.queryByTestId("end-user-shell")).toBeNull();
  });
});
