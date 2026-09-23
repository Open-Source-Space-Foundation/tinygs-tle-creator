/*
 * tinygs-launch JOB: the program the LaunchDaemons run.
 *
 * macOS privacy controls (TCC) block a LaunchDaemon from writing to external
 * volumes unless the *responsible* program has Full Disk Access. Granting that
 * to /bin/bash would give every bash script on the machine unrestricted disk
 * access, so the daemons run this small binary instead, and only it is granted
 * Full Disk Access.
 *
 * It runs exactly one of the pipeline's own scripts (JOB = cycle | details |
 * daily), resolved relative to this binary's location
 * (<repo>/deploy/bin/tinygs-launch -> <repo>/scripts/JOB.sh). It spawns bash as
 * a *child* and waits instead of exec'ing it: a child stays attributed to this
 * binary for TCC, whereas exec would make the process /bin/bash itself.
 *
 * Build: make launcher (ad-hoc signed). Rebuilding changes the code hash, so
 * Full Disk Access has to be re-granted afterwards.
 */
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;

static const char *JOBS[] = {"cycle", "details", "daily", NULL};

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s cycle|details|daily\n", argv[0]);
        return 64;
    }
    int ok = 0;
    for (const char **j = JOBS; *j; j++)
        if (strcmp(argv[1], *j) == 0) ok = 1;
    if (!ok) {
        fprintf(stderr, "tinygs-launch: unknown job '%s'\n", argv[1]);
        return 64;
    }

    char exe[PATH_MAX], real[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) {
        perror("tinygs-launch: resolving own path");
        return 70;
    }
    /* <repo>/deploy/bin/tinygs-launch -> <repo> */
    char *repo = dirname(dirname(dirname(real)));
    if (snprintf(script, sizeof script, "%s/scripts/%s.sh", repo, argv[1]) >=
        (int)sizeof script) {
        fprintf(stderr, "tinygs-launch: path too long\n");
        return 70;
    }

    char *args[] = {"/bin/bash", script, NULL};
    pid_t pid;
    int rc = posix_spawn(&pid, "/bin/bash", NULL, NULL, args, environ);
    if (rc != 0) {
        fprintf(stderr, "tinygs-launch: spawn %s: %s\n", script, strerror(rc));
        return 71;
    }
    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("tinygs-launch: waitpid");
            return 71;
        }
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : 0);
}
