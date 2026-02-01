from vm_manager import VmManager, VmConfig

def main():
    vm = VmManager(work_dir="vms")
    vm.connect()
    print("libvirt OK")

    cfg = VmConfig(
        name="testvm",
        memory_mib=1024,
        vcpus=1,
        disk_size_gb=10,
        network_name="default",
    )

    vm.create_and_start(cfg, recreate=True)

    ip = vm.wait_for_ip(cfg, timeout_s=120)
    print("*** IP:", ip)

if __name__ == "__main__":
    main()
