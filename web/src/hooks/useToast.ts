import { useCallback, useEffect, useRef, useState } from "react";

export function useToast(duration = 3000) {
    const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);
    const timeoutRef = useRef<number | null>(null);

    const showToast = useCallback(
        (message: string, type: "success" | "error") => {
            setToast({ message, type });
            if (timeoutRef.current) {
                window.clearTimeout(timeoutRef.current);
            }
            timeoutRef.current = window.setTimeout(() => {
                setToast(null);
                timeoutRef.current = null;
            }, duration);
        },
        [duration],
    );

    useEffect(
        () => () => {
            if (timeoutRef.current) {
                window.clearTimeout(timeoutRef.current);
            }
        },
        [],
    );

    return { toast, showToast };
}
