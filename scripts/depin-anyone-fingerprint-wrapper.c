#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/types.h>
#include <pwd.h>
#include <grp.h>

#define DOCKER_BIN "/usr/bin/docker"
/* Fixed target: the `anyone` container's own Tor-style fingerprint file, which
   holds "<nickname> <40-hex fingerprint>". The path is a literal — this wrapper
   takes no arguments at all, so there is nothing for a caller to inject. */
#define ANYONE_CONTAINER "anyone"
#define FINGERPRINT_PATH "/var/lib/anon/fingerprint"

static void die(const char *msg) {
    fprintf(stderr, "ERROR: %s\n", msg);
    exit(1);
}

int main(int argc, char *argv[]) {
    (void)argv;
    /* No arguments are accepted. Anything beyond argv[0] is a caller error and
       is rejected outright — the container name and path are hardcoded above. */
    if (argc != 1) {
        fprintf(stderr, "ERROR: this wrapper takes no arguments\n");
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

    /* Acquire root to reach the Docker socket (required for docker exec). The
       exec'd docker process runs as root — unavoidable, the socket requires it
       — but the fixed argv below is the actual boundary: the wrapper can only
       ever cat this one path inside this one container. */
    if (setgroups(0, NULL) != 0 || setegid(0) != 0 || seteuid(0) != 0)
        die("failed to acquire root privileges");

    /* Fixed argv, absolute binary path, no shell, no interpolation, no PATH or
       environment trust. `cat` writes the file to stdout; docker's own
       diagnostics (missing container/file, etc.) go to stderr and a non-zero
       exit code, so the caller can distinguish content from failure. */
    char *docker_argv[] = {
        (char *)DOCKER_BIN, "exec", (char *)ANYONE_CONTAINER,
        "cat", (char *)FINGERPRINT_PATH, NULL,
    };
    execv(DOCKER_BIN, docker_argv);

    fprintf(stderr, "ERROR: execv %s failed: %s\n", DOCKER_BIN, strerror(errno));
    return 1;
}
