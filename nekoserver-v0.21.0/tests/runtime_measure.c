#include <sys/types.h>
#include <sys/wait.h>
#include <sys/resource.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
int main(int argc,char **argv){if(argc<3)return 2;pid_t child=fork();if(child<0)return 2;if(child==0){execvp(argv[2],argv+2);_exit(127);}int status;struct rusage usage;if(wait4(child,&status,0,&usage)<0)return 2;FILE *file=fopen(argv[1],"w");if(!file)return 2;fprintf(file,"%ld\n",usage.ru_maxrss);fclose(file);return WIFEXITED(status)?WEXITSTATUS(status):1;}
