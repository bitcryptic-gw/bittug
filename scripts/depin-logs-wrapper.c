#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/types.h>
#include <pwd.h>

#define DOCKER_BIN "/usr/bin/docker"
/* Must stay in sync with DEPIN_LOG_LINES in gateway-ui/main.py (50). */
#define LOG_TAIL   "50"

/* Hardcoded allowlist — the security boundary of this wrapper. Only these
   exact container names may be passed to docker logs. strcmp exact match;
   no regex, no partial matches, no prefix matching. */
static const char *ALLOWED_PROJECTS[] = {
    "honeygain",
    "urnetwork",
    "myst",
    "anyone",
};
#define ALLOWED_COUNT (sizeof(ALLOWED_PROJECTS) / sizeof(ALLOWED_PROJECTS[0]))

static int is_allowed(const char *name) {
    for (size_t i = 0; i < ALLOWED_COUNT; i++) {
        if (strcmp(ALLOWED_PROJECTS[i], name) == 0)
            return 1;
    }
    return 0;
}

static void die(const char *msg) {
    fprintf(stderr, "ERROR: %s\n", msg);
    exit(1);
}

int main(int argc, char *argv[]) {
    /* Validate the single argument against the hardcoded allowlist first —
       nothing else happens until this passes. Missing/empty/unknown inputs
       are rejected outright; bad values are never sanitized or escaped. */
    if (argc != 2 || argv[1][0] == '\0' || !is_allowed(argv[1])) {
        fprintf(stderr, "ERROR: invalid project name — must be one of: "
                        "honeygain, urnetwork, myst, anyone\n");
        return 1;
    }

    /* Only the gateway-ui user may invoke this wrapper. */
    struct passwd *pw = getpwnam("gateway-ui");
    if (!pw) {
        fprintf(stderr, "ERROR: gateway-ui user not found on system\n");
        return 1;
    }
    if (getuid() != pw->pw_uid) {
        fprintf(stderr, "ERROR: only gateway-ui user may invoke this wrapper\n");
        return 1;
    }

    /* Acquire root to reach the Docker socket (required for docker logs).
       The exec'd docker process itself runs as root — unavoidable, the
       socket requires it — but the allowlist above is the actual boundary:
       the wrapper cannot be used to read any other container. */
    if (setgroups(0, NULL) != 0 || setegid(0) != 0 || seteuid(0) != 0)
        die("failed to acquire root privileges");

    /* Fixed argv, absolute binary path, no shell, no interpolation, no
       PATH or environment trust. */
    char *docker_argv[] = {
        (char *)DOCKER_BIN, "logs", "--tail", LOG_TAIL, argv[1], NULL,
    };
    execv(DOCKER_BIN, docker_argv);

    fprintf(stderr, "ERROR: execv %s failed: %s\n", DOCKER_BIN, strerror(errno));
    return 1;
}
