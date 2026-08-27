#import <AppKit/AppKit.h>

/*
 * Qt 6.11's QNSView accessibility bridge can dereference stale
 * QAccessible objects while macOS is copying the hierarchy. Keep one safe
 * content element visible to AX clients, but never ask Qt to build its child
 * hierarchy. Returning valid values here is important: returning NULL for
 * every legacy attribute makes callers report AXError.notImplemented.
 */

void *vpstitch_empty_accessible_children(void *raw_self, void *selector) {
    (void)raw_self;
    (void)selector;
    return (__bridge void *)@[];
}

void *vpstitch_safe_accessibility_attribute(
    void *raw_self,
    void *selector,
    void *raw_attribute
) {
    (void)selector;
    NSView *view = (__bridge NSView *)raw_self;
    NSString *attribute = (__bridge NSString *)raw_attribute;

    if ([attribute isEqualToString:NSAccessibilityChildrenAttribute]) {
        return (__bridge void *)@[];
    }
    if ([attribute isEqualToString:NSAccessibilityRoleAttribute]) {
        return (__bridge void *)NSAccessibilityGroupRole;
    }
    if ([attribute isEqualToString:NSAccessibilityRoleDescriptionAttribute]) {
        NSString *description = NSAccessibilityRoleDescription(
            NSAccessibilityGroupRole,
            nil
        );
        return (__bridge void *)description;
    }
    if ([attribute isEqualToString:NSAccessibilityTitleAttribute] ||
        [attribute isEqualToString:NSAccessibilityDescriptionAttribute]) {
        return (__bridge void *)@"VP Stitch workspace";
    }
    if ([attribute isEqualToString:NSAccessibilityEnabledAttribute]) {
        return (__bridge void *)@YES;
    }
    if ([attribute isEqualToString:NSAccessibilityFocusedAttribute]) {
        return (__bridge void *)@NO;
    }
    if ([attribute isEqualToString:NSAccessibilityParentAttribute] ||
        [attribute isEqualToString:NSAccessibilityWindowAttribute] ||
        [attribute isEqualToString:NSAccessibilityTopLevelUIElementAttribute]) {
        return (__bridge void *)view.window;
    }
    if ([attribute isEqualToString:NSAccessibilityPositionAttribute] ||
        [attribute isEqualToString:NSAccessibilitySizeAttribute]) {
        NSRect screen_rect = NSZeroRect;
        if (view.window != nil) {
            NSRect window_rect = [view convertRect:view.bounds toView:nil];
            screen_rect = [view.window convertRectToScreen:window_rect];
        }
        if ([attribute isEqualToString:NSAccessibilityPositionAttribute]) {
            return (__bridge void *)[NSValue valueWithPoint:screen_rect.origin];
        }
        return (__bridge void *)[NSValue valueWithSize:screen_rect.size];
    }
    return NULL;
}

void *vpstitch_safe_accessibility_hit_test(
    void *raw_self,
    void *selector,
    NSPoint point
) {
    (void)selector;
    (void)point;
    return raw_self;
}
