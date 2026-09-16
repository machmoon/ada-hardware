import {
  Settings,
  PowerIcon,
  CircuitBoardIcon,
  CableIcon,
  PlugZapIcon,
  TerminalIcon,
} from "lucide-react";
import { invoke } from "@tauri-apps/api/core";

/**
 * The dashboard window's navigation. Only the Ada surfaces remain: the
 * Pluely chat verticals (chats, system prompts, responses, screenshot, audio,
 * dev space, the old dashboard) were removed with the chat product, along
 * with the license-gated support link and upstream's promotional footer.
 */
export const useMenuItems = () => {
  const menu: {
    icon: React.ElementType;
    label: string;
    href: string;
    count?: number;
  }[] = [
    { icon: CircuitBoardIcon, label: "Board", href: "/workbench" },
    { icon: CableIcon, label: "Engine", href: "/engine" },
    { icon: PlugZapIcon, label: "Connections", href: "/integrations" },
    { icon: Settings, label: "Settings", href: "/settings" },
    { icon: TerminalIcon, label: "Logs", href: "/console" },
  ];

  const footerItems = [
    {
      icon: PowerIcon,
      label: "Quit Ada",
      action: async () => {
        await invoke("exit_app");
      },
    },
  ];

  const footerLinks: {
    title: string;
    icon: React.ElementType;
    link: string;
  }[] = [];

  return {
    menu,
    footerItems,
    footerLinks,
  };
};
