#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <pwd.h>
#include <grp.h>
#include <errno.h>
#include <signal.h>

#define REPO_DIR "/opt/gateway"
#define ALLOWED_UNITS \
    "pktfwd.service,gateway-rs.service,gateway-ui.service," \
    "readsb.service,wingbits.service,tailscaled.service"

static int is_allowed(const char *name) {
    const char *p = ALLOWED_UNITS;
    while (*p) {
        const char *end = strchr(p, ',');
        size_t len = end ? (size_t)(end - p) : strlen(p);
        if (strncmp(p, name, len) == 0 && name[len] == '\0')
            return 1;
        p = end ? end + 1 : end;
    }
    return 0;
}

/* Run argv via execvp, inheriting stdin/stdout/stderr. Return exit code. */
static int run(char *const argv[]) {
    pid_t pid = fork();
    if (pid == -1) return -1;
    if (pid == 0) {
        execvp(argv[0], argv);
        _exit(127);
    }
    int status;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return -1;
}

/* Run argv via execvp, capture stdout into buf (up to bufsz-1 bytes, NUL-terminated).
   Stderr passes through to parent. Return exit code. */
static int run_capture(char *const argv[], char *buf, size_t bufsz) {
    int pipefd[2];
    if (pipe(pipefd) == -1) return -1;
    pid_t pid = fork();
    if (pid == -1) {
        close(pipefd[0]);
        close(pipefd[1]);
        return -1;
    }
    if (pid == 0) {
        close(pipefd[0]);
        dup2(pipefd[1], STDOUT_FILENO);
        close(pipefd[1]);
        execvp(argv[0], argv);
        _exit(127);
    }
    close(pipefd[1]);
    ssize_t n = read(pipefd[0], buf, bufsz - 1);
    if (n > 0) buf[n] = '\0';
    else buf[0] = '\0';
    close(pipefd[0]);
    int status;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return -1;
}

static void die(const char *call, const char *ctx) {
    fprintf(stderr, "ERROR: %s failed at %s: %s\n", call, ctx, strerror(errno));
    exit(1);
}

int main(int argc, char *argv[]) {
    struct passwd *pw = getpwnam("gateway-ui");
    if (!pw) {
        fprintf(stderr, "ERROR: gateway-ui user not found on system\n");
        return 1;
    }
    if (getuid() != pw->pw_uid) {
        fprintf(stderr, "ERROR: only gateway-ui user may invoke this wrapper\n");
        return 1;
    }

    /* Ignore SIGPIPE — when main.py's SSE connection is closed during
       gateway-ui.service restart, writes to stdout will return EPIPE
       instead of killing the process, so we can finish writing
       /etc/gateway-version and exit with a meaningful return code. */
    signal(SIGPIPE, SIG_IGN);

    if (argc < 2) {
        fprintf(stderr, "ERROR: usage: ota-update-wrapper --changes | <service1,service2,...>\n");
        return 1;
    }

    /* ── Acquire root privilege (setuid binary starts euid=0, make it real) ── */
    if (setgroups(0, NULL) != 0)
        die("setgroups(0)", "initial privilege acquisition");
    if (setegid(0) != 0)
        die("setegid(0)", "initial privilege acquisition");
    if (seteuid(0) != 0)
        die("seteuid(0)", "initial privilege acquisition");

    /* Determine repo owner from /opt/gateway directory stat */
    struct stat st;
    if (stat(REPO_DIR, &st) != 0) {
        fprintf(stderr, "ERROR: cannot stat %s: %s\n", REPO_DIR, strerror(errno));
        return 1;
    }
    uid_t repo_owner = st.st_uid;

    /* chdir to repo */
    if (chdir(REPO_DIR) != 0) {
        fprintf(stderr, "ERROR: cannot chdir to %s: %s\n", REPO_DIR, strerror(errno));
        return 1;
    }

    /* ── --changes mode ─────────────────────────────────────────────────────── */
    if (strcmp(argv[1], "--changes") == 0) {
        if (seteuid(repo_owner) != 0)
            die("seteuid(repo_owner)", "--changes mode");
        if (setegid(0) != 0)
            die("setegid(0)", "--changes mode");

        int fetch_rc = run((char *[]){"/usr/bin/git", "fetch", "origin", NULL});
        if (fetch_rc != 0) {
            fprintf(stderr, "ERROR: git fetch origin failed (exit %d)\n", fetch_rc);
            return 1;
        }

        char diff_buf[65536];
        int diff_rc = run_capture(
            (char *[]){"/usr/bin/git", "diff", "--name-only", "HEAD..origin/main", NULL},
            diff_buf, sizeof(diff_buf));
        if (diff_rc != 0) {
            fprintf(stderr, "ERROR: git diff failed (exit %d)\n", diff_rc);
            return 1;
        }
        printf("%s", diff_buf);
        if (diff_buf[0] && diff_buf[strlen(diff_buf) - 1] != '\n')
            putchar('\n');
        return 0;
    }

    /* ── Update mode (default) ──────────────────────────────────────────────── */

    /* Parse and validate service list */
    char svc_buf[1024];
    strncpy(svc_buf, argv[1], sizeof(svc_buf) - 1);
    svc_buf[sizeof(svc_buf) - 1] = '\0';
    char *svc_list[32];
    int svc_count = 0;
    char *token = strtok(svc_buf, ",");
    while (token && svc_count < 32) {
        while (*token == ' ') token++;
        char *end = token + strlen(token);
        while (end > token && end[-1] == ' ') end--;
        *end = '\0';
        if (*token == '\0') { token = strtok(NULL, ","); continue; }
        if (!is_allowed(token)) {
            fprintf(stderr, "ERROR: not allowed: %s\n", token);
            return 1;
        }
        svc_list[svc_count++] = token;
        token = strtok(NULL, ",");
    }

    if (svc_count == 0) {
        fprintf(stderr, "ERROR: at least one service required\n");
        return 1;
    }

    /* Line-buffer stdout for SSE streaming */
    setvbuf(stdout, NULL, _IOLBF, 0);

    /* Capture pre-pull HEAD */
    char pre_head[128] = "";
    run_capture((char *[]){"/usr/bin/git", "rev-parse", "HEAD", NULL},
                pre_head, sizeof(pre_head));
    if (pre_head[0]) {
        char *nl = strchr(pre_head, '\n');
        if (nl) *nl = '\0';
    }

    /* git pull as repo owner */
    if (seteuid(repo_owner) != 0)
        die("seteuid(repo_owner)", "pre-pull privilege drop");
    if (setegid(0) != 0)
        die("setegid(0)", "pre-pull privilege drop");

    int pull_rc = run((char *[]){"/usr/bin/git", "pull", NULL});

    /* Restore root */
    if (seteuid(0) != 0)
        die("seteuid(0)", "post-pull privilege restore");
    if (setegid(0) != 0)
        die("setegid(0)", "post-pull privilege restore");

    if (pull_rc != 0) {
        fprintf(stderr, "ERROR: git pull failed (exit %d)\n", pull_rc);
        return 1;
    }

    /* Sync provisioning state (sudoers grants, user setup, file ownership).
       Non-fatal: failure logs a warning but does not abort the OTA. */
    {
        char *sync_argv[] = {"/bin/bash", "-p", REPO_DIR "/scripts/sync-provisioning.sh", NULL};
        int sync_rc = run(sync_argv);
        if (sync_rc != 0) {
            fprintf(stderr, "WARNING: sync-provisioning.sh failed (exit %d) — "
                            "timezone/hostname features may need a re-run of OTA\n", sync_rc);
        }
    }

    /* Capture post-pull HEAD */
    char post_head[128] = "";
    run_capture((char *[]){"/usr/bin/git", "rev-parse", "HEAD", NULL},
                post_head, sizeof(post_head));
    if (post_head[0]) {
        char *nl = strchr(post_head, '\n');
        if (nl) *nl = '\0';
    }

    /* Fetch tags so git describe sees the latest release tag */
    if (seteuid(repo_owner) != 0)
        die("seteuid(repo_owner)", "tag fetch privilege drop");
    if (setegid(0) != 0)
        die("setegid(0)", "tag fetch privilege drop");
    run((char *[]){"/usr/bin/git", "fetch", "--tags", NULL});
    if (seteuid(0) != 0)
        die("seteuid(0)", "tag fetch privilege restore");
    if (setegid(0) != 0)
        die("setegid(0)", "tag fetch privilege restore");

    /* Capture version string */
    char version[256] = "unknown";
    if (seteuid(repo_owner) != 0)
        die("seteuid(repo_owner)", "git describe privilege drop");
    if (setegid(0) != 0)
        die("setegid(0)", "git describe privilege drop");
    char describe_buf[256] = "";
    int describe_rc = run_capture(
        (char *[]){"/usr/bin/git", "-C", REPO_DIR, "describe", "--tags", "--always", NULL},
        describe_buf, sizeof(describe_buf));
    if (seteuid(0) != 0)
        die("seteuid(0)", "git describe privilege restore");
    if (setegid(0) != 0)
        die("setegid(0)", "git describe privilege restore");

    if (describe_rc == 0 && describe_buf[0]) {
        char *nl = strchr(describe_buf, '\n');
        if (nl) *nl = '\0';
        strncpy(version, describe_buf, sizeof(version) - 1);
        version[sizeof(version) - 1] = '\0';
    } else {
        fprintf(stderr, "WARNING: git describe failed (exit %d) — /etc/gateway-version not updated\n", describe_rc);
    }

    /* Write version file immediately — before provisioning, wrapper
       recompilation, or service restarts.  The browser's SSE-disconnect
       reload fires at T+5s from gateway-ui restart; if we defer this
       write until after the restart + 15s health-poll, the freshly
       reloaded page sees the old version for up to an hour. */
    {
        FILE *vf = fopen("/etc/gateway-version", "w");
        if (vf) {
            fprintf(vf, "%s\n", version);
            fclose(vf);
        } else {
            fprintf(stderr, "WARNING: cannot write /etc/gateway-version: %s\n", strerror(errno));
        }
        printf("VERSION:%s\n", version);
    }

    /* ── Recompile all setuid wrappers ──────────────────────────────────────── */
    /* Delegates to install-wrappers.sh which is the single source of truth.
       Fix up HOME and TMPDIR — the gateway-ui user has no real home directory,
       which breaks gcc/ld even when running as root (privilege syscalls don't
       touch environment variables). */
    if (setenv("HOME", "/root", 1) != 0)
        die("setenv(HOME)", "pre-wrapper environment fixup");
    if (setenv("TMPDIR", "/tmp", 1) != 0)
        die("setenv(TMPDIR)", "pre-wrapper environment fixup");
    int wrapper_rc = run((char *[]){"/bin/bash", "-p", REPO_DIR "/scripts/install-wrappers.sh", NULL});

    /* Write diff to stdout */
    if (pre_head[0] && post_head[0] && strcmp(pre_head, post_head) != 0) {
        char *diff_argv[] = {
            "/usr/bin/git", "diff", "--name-only", pre_head, post_head, NULL
        };
        char diff_buf[8192];
        int diff_rc = run_capture(diff_argv, diff_buf, sizeof(diff_buf));
        if (diff_rc == 0 && diff_buf[0]) {
            fputs(diff_buf, stdout);
            if (diff_buf[strlen(diff_buf) - 1] != '\n')
                putchar('\n');
        }
    } else {
        printf("(no changes)\n");
    }

    // TODO: move to a sibling cgroup under system.slice rather than
    // the root cgroup, so the OTA process remains under normal systemd
    // resource accounting and cgroup hygiene (root cgroup is a
    // deliberate short-term simplification — tracked in project backlog).
    /* Escape gateway-ui.service's cgroup before restarting any services.
       gateway-ui.service has no explicit KillMode= (defaults to
       control-group), so systemctl restart gateway-ui.service sends
       SIGTERM to every process in its cgroup — including us, since we
       were spawned as its child. We must move out before restarting
       anything, or we (and our own exit code) get killed along with it. */
    {
        FILE *cgf = fopen("/sys/fs/cgroup/cgroup.procs", "w");
        if (cgf) {
            fprintf(cgf, "0\n");
            fclose(cgf);
        } else {
            fprintf(stderr, "WARNING: could not detach from gateway-ui.service's "
                            "cgroup (%s) — restarting gateway-ui.service may kill "
                            "this process before it can report a final result\n",
                            strerror(errno));
        }
    }

    /* Restart services as root */
    int restart_failed = 0;
    for (int i = 0; i < svc_count; i++) {
        int rc;
        if (strcmp(svc_list[i], "gateway-ui.service") == 0) {
            /* gateway-ui.service is restarting its own parent process
               (this binary was spawned by main.py). A normal blocking
               systemctl restart deadlocks: systemd waits for the old
               main.py to stop gracefully, but main.py is still reading
               our stdout via the SSE stream. Use --no-block to queue
               the restart asynchronously; the cgroup detachment above
               ensures we survive the restart. */
            rc = run((char *[]){"/usr/bin/systemctl", "restart", "--no-block", svc_list[i], NULL});
            printf("restarted %s (exit %d)\n", svc_list[i], rc);

            /* --no-block only confirms the restart was queued, not that
               the service actually came back up. Poll briefly so we can
               still report a meaningful result for gateway-ui.service. */
            int gw_ui_healthy = 0;
            for (int poll = 0; poll < 30; poll++) {
                usleep(500000);
                char active_buf[64] = "";
                int active_rc = run_capture(
                    (char *[]){"/usr/bin/systemctl", "is-active", "gateway-ui.service", NULL},
                    active_buf, sizeof(active_buf));
                if (active_rc == 0 && strncmp(active_buf, "active", 6) == 0) {
                    gw_ui_healthy = 1;
                    break;
                }
            }
            if (!gw_ui_healthy) {
                fprintf(stderr, "WARNING: gateway-ui.service did not report 'active' within 15s of restart\n");
                restart_failed = 1;
            }
        } else {
            rc = run((char *[]){"/usr/bin/systemctl", "restart", svc_list[i], NULL});
            printf("restarted %s (exit %d)\n", svc_list[i], rc);
        }
        if (rc != 0)
            restart_failed = 1;
    }

    /* Overall success: wrappers compiled AND all service restarts succeeded */
    int overall_ok = (wrapper_rc == 0 && !restart_failed);

    if (wrapper_rc != 0)
        fprintf(stderr, "ERROR: wrapper recompilation failed (exit %d)\n", wrapper_rc);
    if (restart_failed)
        fprintf(stderr, "ERROR: one or more service restarts failed\n");

    return overall_ok ? 0 : 1;
}
