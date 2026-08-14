#include <stdio.h>
#include <unistd.h>
#include <sys/types.h>
#include <pwd.h>

#define WINGBITS_BIN "/usr/local/bin/wingbits"

int main(void) {
    struct passwd *pw = getpwnam("gateway-ui");
    if (!pw) {
        fprintf(stderr, "ERROR: gateway-ui user not found on system\n");
        return 1;
    }
    if (getuid() != pw->pw_uid) {
        fprintf(stderr, "ERROR: only gateway-ui user may invoke this wrapper\n");
        return 1;
    }

    setuid(0);
    setgid(0);

    execl(WINGBITS_BIN, WINGBITS_BIN, "status", (char *)NULL);

    fprintf(stderr, "ERROR: failed to execute %s\n", WINGBITS_BIN);
    return 1;
}
