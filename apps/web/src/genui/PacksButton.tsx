// Domain-pack manager (M9): a sidebar entry point that opens a modal to
// discover published packs, install / uninstall them, and deploy their
// provisioning templates. Mutations are disabled unless the backend has
// NOESIS_PACKS_ADMIN enabled.

import { useState } from "react";
import { Boxes, Check, Download, Loader2, Trash2, X } from "lucide-react";
import { Button } from "../components/ui/button";
import { Badge } from "../components/ui/badge";
import {
  useDeployTemplate,
  useInstallPack,
  usePackDiscovery,
  usePackTemplates,
  useUninstallPack,
} from "./usePacks";
import PackPublish from "./PackPublish";

function PacksModal({ onClose }: { onClose: () => void }) {
  const discovery = usePackDiscovery();
  const templates = usePackTemplates();
  const install = useInstallPack();
  const uninstall = useUninstallPack();
  const deploy = useDeployTemplate();
  const [deployed, setDeployed] = useState<Record<string, boolean>>({});

  const admin = discovery.data?.admin_enabled ?? false;
  const packs = discovery.data?.packs ?? [];
  const busy = install.isPending || uninstall.isPending;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border bg-[#070d13] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 border-b px-5 py-3.5">
          <Boxes className="size-4 text-primary" />
          <span className="font-grotesk text-sm font-semibold">Domain packs</span>
          {!admin ? (
            <Badge variant="outline" className="border-amber-400/30 bg-amber-400/10 text-amber-400" title="Set NOESIS_PACKS_ADMIN=on to install">
              READ-ONLY
            </Badge>
          ) : null}
          <span className="flex-1" />
          <button onClick={onClose} className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground">
            <X className="size-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {discovery.isLoading ? (
            <div className="py-8 text-center font-mono text-[11px] text-muted-foreground">loading packs…</div>
          ) : discovery.isError ? (
            <div className="py-8 text-center font-mono text-[11px] text-muted-foreground">
              the pack API is unavailable
            </div>
          ) : packs.length === 0 ? (
            <div className="py-8 text-center font-mono text-[11px] text-muted-foreground">
              no packs published to the registry
            </div>
          ) : (
            <div className="flex flex-col gap-2.5">
              {packs.map((p) => {
                const installed = !!p.installed_version;
                return (
                  <div key={p.name} className="rounded-lg border bg-secondary/30 px-3.5 py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-grotesk text-[13px] font-semibold">{p.name}</span>
                      <span className="font-mono text-[10px] text-muted-foreground/60">
                        v{p.latest_version ?? "?"}
                      </span>
                      {installed ? (
                        <Badge variant="live" title={`Installed v${p.installed_version}`}>INSTALLED</Badge>
                      ) : null}
                      <span className="flex-1" />
                      {installed ? (
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-6 rounded-md px-2 font-mono text-[10px]"
                          disabled={!admin || busy}
                          onClick={() => uninstall.mutate(p.name)}
                          title={admin ? "Uninstall this pack" : "Enable NOESIS_PACKS_ADMIN to uninstall"}
                        >
                          <Trash2 className="!size-3" /> REMOVE
                        </Button>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-6 rounded-md px-2 font-mono text-[10px]"
                          disabled={!admin || busy}
                          onClick={() => install.mutate({ name: p.name })}
                          title={admin ? "Install this pack" : "Enable NOESIS_PACKS_ADMIN to install"}
                        >
                          {install.isPending ? <Loader2 className="!size-3 animate-spin" /> : <Download className="!size-3" />}
                          INSTALL
                        </Button>
                      )}
                    </div>
                    {p.description ? (
                      <p className="mt-1.5 text-[11.5px] leading-snug text-muted-foreground">{p.description}</p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}

          {/* Provisioning templates enabled by installed packs. */}
          {templates.data && templates.data.length > 0 ? (
            <div className="mt-5">
              <div className="mb-2 font-mono text-[9.5px] tracking-[0.16em] text-muted-foreground/60">
                PROVISIONING TEMPLATES
              </div>
              <div className="flex flex-col gap-2">
                {templates.data.map((t) => (
                  <div key={t.name} className="flex items-center gap-2 rounded-lg border bg-secondary/30 px-3.5 py-2.5">
                    <span className="font-mono text-[12px]">{t.name}</span>
                    <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">{t.description}</span>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-6 rounded-md px-2 font-mono text-[10px]"
                      disabled={!admin || deploy.isPending}
                      onClick={() =>
                        deploy.mutate(t.name, { onSuccess: () => setDeployed((d) => ({ ...d, [t.name]: true })) })
                      }
                      title={admin ? "Deploy this template's knowledge graph" : "Enable NOESIS_PACKS_ADMIN to deploy"}
                    >
                      {deployed[t.name] ? <Check className="!size-3 text-emerald-400" /> : null}
                      {deployed[t.name] ? "DEPLOYED" : "DEPLOY KG"}
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {/* Author and publish a new pack to the registry (M9). */}
          <PackPublish admin={admin} />
        </div>
      </div>
    </div>
  );
}

export default function PacksButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className="h-7 w-full justify-start gap-2 rounded-md px-2.5 font-mono text-[10.5px] text-muted-foreground"
        onClick={() => setOpen(true)}
        title="Discover and install domain packs"
      >
        <Boxes className="!size-3.5" /> PACKS
      </Button>
      {open ? <PacksModal onClose={() => setOpen(false)} /> : null}
    </>
  );
}
