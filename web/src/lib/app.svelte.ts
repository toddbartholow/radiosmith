// Shared state: service status, ports, backups, radio jobs, and which radio and view is selected.

import {
  api,
  ApiError,
  errorText,
  type BackupInfo,
  type IdentifyResult,
  type Job,
  type Port,
  type RadioId,
  type Status,
} from "./api";

export type RadioView = "channels" | "edit" | "history";
export type DraftSection = "channels" | "zones" | "scan_lists" | "edits" | "import" | "review";

export const RADIO_IDS: RadioId[] = ["dm32", "rt950"];
export const RADIO_NAMES: Record<RadioId, string> = { dm32: "Baofeng DM-32UV", rt950: "Radtel RT-950 PRO" };
export const SHORT_NAMES: Record<RadioId, string> = { dm32: "DM-32UV", rt950: "RT-950 PRO" };
export const READ_TIME: Record<RadioId, string> = { dm32: "about 4 minutes", rt950: "about 30 seconds" };

const VIEWS: RadioView[] = ["channels", "edit", "history"];
const SECTIONS: DraftSection[] = ["channels", "zones", "scan_lists", "edits", "import", "review"];

export interface ConnectionCheck {
  checking: boolean;
  result: IdentifyResult | null;
  error: string;
  at: string;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** How long each job poll waits on the server; it answers early on each new log line. */
const JOB_POLL_WAIT_S = 20;

const isRadio = (s: string | undefined): s is RadioId => s === "dm32" || s === "rt950";

class AppState {
  status = $state<Status | null>(null);
  statusError = $state("");
  ports = $state<Port[]>([]);
  portsError = $state("");
  portsLoading = $state(false);
  backups = $state<BackupInfo[]>([]);
  backupsError = $state("");
  backupsLoaded = $state(false);
  jobs = $state<Record<string, Job>>({});
  /** Read jobs the user closed in the radio header. */
  dismissed = $state<string[]>([]);

  /** Port chosen for each radio; empty means Auto (the port that last answered as it). */
  port = $state<Record<RadioId, string>>({ dm32: "", rt950: "" });
  connection = $state<Record<RadioId, ConnectionCheck>>({
    dm32: { checking: false, result: null, error: "", at: "" },
    rt950: { checking: false, result: null, error: "", at: "" },
  });
  readError = $state<Record<RadioId, string>>({ dm32: "", rt950: "" });
  startingRead = $state<RadioId | null>(null);

  // -- navigation --
  radio = $state<RadioId>("dm32");
  view = $state<RadioView>("channels");
  draft = $state<string | null>(null);
  draftSection = $state<DraftSection>("channels");
  /** Backup open in History. */
  backup = $state<string | null>(null);

  #watching = new Set<string>();

  get busy(): boolean {
    return !!this.status?.busy || this.runningJobs.length > 0 || RADIO_IDS.some((r) => this.connection[r].checking);
  }

  get runningJobs(): Job[] {
    return Object.values(this.jobs).filter((j) => j.status === "running");
  }

  /** Write, dry run and verify jobs for one edit set, newest first. */
  jobsForDraft(draftId: string): Job[] {
    return Object.values(this.jobs)
      .filter((j) => j.draft === draftId)
      .sort((a, b) => b.started_utc.localeCompare(a.started_utc));
  }

  jobsFor(radio: RadioId): Job[] {
    return Object.values(this.jobs)
      .filter((j) => j.radio === radio)
      .sort((a, b) => b.started_utc.localeCompare(a.started_utc));
  }

  backupsFor(radio: RadioId): BackupInfo[] {
    return this.backups.filter((b) => b.radio === radio);
  }

  /** Newest complete backup: what the radio held when it was last read. */
  newestBackup(radio: RadioId): BackupInfo | undefined {
    return this.backups.find((b) => b.radio === radio && b.complete);
  }

  /** The port that last answered as this radio, if it is plugged in. */
  rememberedPort(radio: RadioId): string | null {
    return this.ports.find((p) => p.last_seen_radio === radio)?.device ?? null;
  }

  /** The port an action will use, for display: the override, else the remembered one. */
  portLabel(radio: RadioId): string {
    return this.port[radio] || this.rememberedPort(radio) || "";
  }

  async refreshStatus(): Promise<void> {
    try {
      this.status = await api.status();
      this.statusError = "";
      for (const id of this.status.jobs_running) {
        if (!this.jobs[id]) this.track(await api.job(id));
      }
    } catch (e) {
      this.statusError = errorText(e);
    }
  }

  async refreshPorts(): Promise<void> {
    this.portsLoading = true;
    try {
      this.ports = await api.ports();
      this.portsError = "";
    } catch (e) {
      this.portsError = errorText(e);
    } finally {
      this.portsLoading = false;
    }
  }

  async refreshBackups(): Promise<void> {
    try {
      this.backups = await api.backups();
      this.backupsError = "";
    } catch (e) {
      this.backupsError = errorText(e);
    } finally {
      this.backupsLoaded = true;
    }
  }

  async loadJobs(): Promise<void> {
    try {
      for (const j of await api.jobs()) this.track(j);
    } catch {
      // status polling reports an unreachable service
    }
  }

  /** Remember a job and keep polling it until it finishes. */
  track(job: Job): void {
    this.jobs[job.id] = job;
    if (job.status === "running") void this.#watch(job.id);
  }

  async #watch(id: string): Promise<void> {
    if (this.#watching.has(id)) return;
    this.#watching.add(id);
    try {
      for (;;) {
        try {
          const job = await api.job(id, JOB_POLL_WAIT_S);
          this.jobs[id] = job;
          if (job.status !== "running") break;
        } catch (e) {
          if (e instanceof ApiError && e.status === 400) break; // the service no longer knows the job
          await sleep(2000);
        }
      }
    } finally {
      this.#watching.delete(id);
      void this.refreshStatus();
      void this.refreshPorts();
      void this.refreshBackups(); // reads, writes and verifies all save backups
    }
  }

  async checkConnection(radio: RadioId): Promise<void> {
    const c = this.connection[radio];
    c.checking = true;
    c.error = "";
    try {
      c.result = await api.identify(radio, this.port[radio]);
      void this.refreshPorts();
    } catch (e) {
      c.result = null;
      c.error = errorText(e);
    } finally {
      c.checking = false;
      c.at = new Date().toISOString();
    }
  }

  async startRead(radio: RadioId): Promise<void> {
    this.startingRead = radio;
    this.readError[radio] = "";
    try {
      this.track(await api.read(radio, this.port[radio]));
      void this.refreshStatus();
    } catch (e) {
      this.readError[radio] = errorText(e);
    } finally {
      this.startingRead = null;
    }
  }

  selectRadio(radio: RadioId): void {
    if (radio === this.radio) return;
    this.radio = radio;
    this.backup = null;
    this.draft = null;
  }

  openDraft(id: string | null, section: DraftSection = "channels", radio: RadioId = this.radio): void {
    this.radio = radio;
    this.view = "edit";
    this.draft = id;
    this.draftSection = section;
  }

  openBackup(path: string): void {
    const radio = path.split("/")[1];
    if (isRadio(radio)) this.radio = radio;
    this.view = "history";
    this.backup = path;
  }

  // -- location hash: #<radio>/channels, #dm32/edit/<draft>/<section>, #<radio>/history/<backup path> --

  toHash(): string {
    let h = `#${this.radio}/${this.view}`;
    if (this.view === "edit" && this.draft) h += `/${this.draft}/${this.draftSection}`;
    if (this.view === "history" && this.backup) h += `/${this.backup}`;
    return h;
  }

  fromHash(hash: string): void {
    const parts = decodeURIComponent(hash.replace(/^#/, "")).split("/").filter(Boolean);
    const [first, second, ...rest] = parts;
    // Links from the earlier layout: #drafts/<id>/<section>, #backups/<path>.
    if (first === "drafts") {
      this.openDraft(second ?? null, SECTIONS.find((s) => s === rest[0]) ?? "channels", "dm32");
      return;
    }
    if (first === "backups") {
      if (second) this.openBackup([second, ...rest].join("/"));
      else this.view = "history";
      return;
    }
    this.radio = isRadio(first) ? first : "dm32";
    this.view = VIEWS.find((v) => v === second) ?? "channels";
    this.draft = null;
    this.backup = null;
    if (this.view === "edit") {
      this.draft = rest[0] || null;
      this.draftSection = SECTIONS.find((s) => s === rest[1]) ?? "channels";
    } else if (this.view === "history" && rest.length) {
      this.backup = rest.join("/");
    }
  }
}

export const app = new AppState();
